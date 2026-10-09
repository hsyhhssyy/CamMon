/* SPDX-License-Identifier: GPL-3.0-or-later
 * CamMon VFS, built against exactly the same source tree as smbd.
 * Physical files are namespace placeholders. All video bytes use the RPC service.
 */
#include "includes.h"
#include "smbd/smbd.h"
#include "lib/util/tevent_unix.h"
#include "lib/util/tevent_ntstatus.h"
#include <jansson.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <arpa/inet.h>

#define CM_MAX_DATA (8 * 1024 * 1024)
struct cm_connection { int socket; };
struct cm_descriptor { int fd; uint64_t token; struct cm_descriptor *next; };
struct cm_file { struct cm_descriptor *descriptors; off_t position; };
struct cm_directory { char *path; int fd; size_t offset; size_t index; json_t *page; struct dirent entry; };

static bool cm_transfer(int fd, void *buffer, size_t length, bool writing)
{
    uint8_t *p = buffer;
    while (length) {
        ssize_t n = writing ? send(fd, p, length, MSG_NOSIGNAL) : recv(fd, p, length, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { errno = EIO; return false; }
        length -= n; p += n;
    }
    return true;
}

static json_t *cm_rpc(struct vfs_handle_struct *h, json_t *request,
                      const void *data, size_t length, void *output, size_t capacity)
{
    struct cm_connection *c = h->data;
    char *encoded = NULL, *response = NULL;
    json_t *result = NULL;
    uint32_t frame[2];
    size_t meta_len, data_len;
    int saved = EIO;
    if (!c || c->socket < 0 || length > CM_MAX_DATA) goto failed;
    encoded = json_dumps(request, JSON_COMPACT);
    if (!encoded) { saved = ENOMEM; goto failed; }
    frame[0] = htonl(strlen(encoded)); frame[1] = htonl(length);
    if (!cm_transfer(c->socket, frame, sizeof(frame), true) ||
        !cm_transfer(c->socket, encoded, strlen(encoded), true) ||
        (length && !cm_transfer(c->socket, (void *)data, length, true)) ||
        !cm_transfer(c->socket, frame, sizeof(frame), false)) goto failed;
    meta_len = ntohl(frame[0]); data_len = ntohl(frame[1]);
    if (meta_len > 65536 || data_len > capacity || data_len > CM_MAX_DATA) goto failed;
    response = malloc(meta_len + 1);
    if (!response) { saved = ENOMEM; goto failed; }
    if (!cm_transfer(c->socket, response, meta_len, false)) goto failed;
    response[meta_len] = 0;
    result = json_loadb(response, meta_len, 0, NULL);
    if (!result) goto failed;
    if (data_len && !cm_transfer(c->socket, output, data_len, false)) goto failed;
    if (!json_is_true(json_object_get(result, "ok"))) {
        saved = json_integer_value(json_object_get(result, "errno"));
        DBG_WARNING("CamMon RPC op=%s path=%s: %s\n", json_string_value(json_object_get(request, "op")),
            json_string_value(json_object_get(request, "path")), json_string_value(json_object_get(result, "error")));
        goto error_response;
    }
    free(encoded); free(response); json_decref(request);
    return result;
failed:
    if (c && c->socket >= 0) { close(c->socket); c->socket = -1; }
error_response:
    free(encoded); free(response); json_decref(request); json_decref(result);
    errno = saved > 0 ? saved : EIO;
    return NULL;
}

static int cm_void(struct vfs_handle_struct *h, json_t *q)
{
    json_t *r = cm_rpc(h, q, NULL, 0, NULL, 0);
    if (!r) return -1;
    json_decref(r); return 0;
}

static const char *cm_relative(struct vfs_handle_struct *h, const char *name)
{
    const char *root = h->conn->connectpath;
    size_t n = strlen(root);
    char resolved[PATH_MAX];
    const char *absolute;
    /* Samba reopens path-reference descriptors through /proc during enumeration. */
    if (strncmp(name, "/proc/self/fd/", 14) == 0) {
        ssize_t length = readlink(name, resolved, sizeof(resolved) - 1);
        if (length < 0 || length >= sizeof(resolved) - 1) { errno = EACCES; return NULL; }
        resolved[length] = 0;
        name = resolved;
    }
    absolute = name[0] == '/' ? name : talloc_asprintf(talloc_tos(), "%s/%s", root, name);
    if (!absolute) { errno = ENOMEM; return NULL; }
    absolute = canonicalize_absolute_path(talloc_tos(), absolute);
    if (!absolute) { errno = ENOMEM; return NULL; }
    if (strncmp(absolute, root, n) || (absolute[n] && absolute[n] != '/')) { errno = EACCES; return NULL; }
    absolute += n;
    return *absolute == '/' ? absolute + 1 : absolute;
}

static const char *cm_atpath(struct vfs_handle_struct *h, const struct files_struct *dir,
                              const struct smb_filename *name)
{
    struct smb_filename *full = full_path_from_dirfsp_atname(talloc_tos(), dir, name);
    if (!full) { errno = ENOMEM; return NULL; }
    if (full->stream_name != NULL) { errno = EACCES; return NULL; }
    return cm_relative(h, full->base_name);
}

static uint64_t cm_fh(struct vfs_handle_struct *h, struct files_struct *fsp)
{
    struct cm_file *file = (struct cm_file *)VFS_FETCH_FSP_EXTENSION(h, fsp);
    struct cm_descriptor *entry;
    if (!file) return 0;
    for (entry = file->descriptors; entry; entry = entry->next)
        if (entry->fd == fsp_get_pathref_fd(fsp)) return entry->token;
    return 0;
}

static void cm_file_destroy(void *data)
{
    struct cm_file *file = data;
    while (file->descriptors) {
        struct cm_descriptor *entry = file->descriptors;
        file->descriptors = entry->next; free(entry);
    }
}

static int cm_connect(struct vfs_handle_struct *h, const char *service, const char *user)
{
    struct cm_connection *c;
    struct sockaddr_un address = { .sun_family = AF_UNIX };
    struct timeval timeout = { .tv_sec = 40 };
    const char *path = lp_parm_const_string(SNUM(h->conn), "cammon", "socket", "/run/cammon/vfs.sock");
    const char *device = lp_parm_const_string(SNUM(h->conn), "cammon", "device", "");
    if (strlen(path) >= sizeof(address.sun_path)) { errno = EINVAL; return -1; }
    if (SMB_VFS_NEXT_CONNECT(h, service, user) < 0) return -1;
    c = talloc_zero(h, struct cm_connection);
    if (!c) { errno = ENOMEM; return -1; }
    c->socket = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (c->socket < 0) return -1;
    setsockopt(c->socket, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(c->socket, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    strlcpy(address.sun_path, path, sizeof(address.sun_path));
    if (connect(c->socket, (struct sockaddr *)&address, sizeof(address)) < 0) {
        close(c->socket); c->socket = -1; return -1;
    }
    h->data = c;
    return cm_void(h, json_pack("{s:s,s:s}", "op", "hello", "device", device));
}

static void cm_disconnect(struct vfs_handle_struct *h)
{
    struct cm_connection *c = h->data;
    if (c && c->socket >= 0) { close(c->socket); c->socket = -1; }
    SMB_VFS_NEXT_DISCONNECT(h);
}

static int cm_openat(struct vfs_handle_struct *h, const struct files_struct *dir,
                     const struct smb_filename *name, struct files_struct *fsp, const struct vfs_open_how *how)
{
    const char *path = cm_atpath(h, dir, name);
    json_t *r;
    struct cm_file *file;
    struct cm_descriptor *entry;
    uint64_t token;
    int fd, saved;
    struct vfs_open_how local = *how;
    if (!path) return -1;
    r = cm_rpc(h, json_pack("{s:s,s:s,s:I}", "op", "open", "path", path, "flags", (json_int_t)how->flags),
               NULL, 0, NULL, 0);
    if (!r) return -1;
    token = json_integer_value(json_object_get(r, "result")); json_decref(r);
    /* The daemon created the placeholder; truncation always applies to the real cache only. */
    local.flags &= ~(O_TRUNC | O_EXCL);
    fd = SMB_VFS_NEXT_OPENAT(h, dir, name, fsp, &local);
    if (fd < 0) {
        saved = errno;
        if (token) cm_void(h, json_pack("{s:s,s:I}", "op", "close", "handle", (json_int_t)token));
        errno = saved; return -1;
    }
    file = VFS_ADD_FSP_EXTENSION(h, fsp, struct cm_file, cm_file_destroy);
    entry = file ? calloc(1, sizeof(*entry)) : NULL;
    if (!entry) {
        close(fd);
        if (token) cm_void(h, json_pack("{s:s,s:I}", "op", "close", "handle", (json_int_t)token));
        errno = ENOMEM; return -1;
    }
    /* Reopening an O_PATH fsp closes its old descriptor after this hook returns. */
    entry->fd = fd; entry->token = token; entry->next = file->descriptors;
    file->descriptors = entry; file->position = 0;
    return fd;
}

static int cm_close(struct vfs_handle_struct *h, struct files_struct *fsp)
{
    uint64_t token = cm_fh(h, fsp);
    struct cm_file *file = VFS_FETCH_FSP_EXTENSION(h, fsp);
    struct cm_descriptor **link = file ? &file->descriptors : NULL;
    if (token) cm_void(h, json_pack("{s:s,s:I}", "op", "close", "handle", (json_int_t)token));
    while (link && *link) {
        if ((*link)->fd == fsp_get_pathref_fd(fsp)) {
            struct cm_descriptor *entry = *link;
            *link = entry->next; free(entry); break;
        }
        link = &(*link)->next;
    }
    return SMB_VFS_NEXT_CLOSE(h, fsp);
}

static int cm_fill_stat(struct vfs_handle_struct *h, const char *path, uint64_t token, SMB_STRUCT_STAT *st)
{
    json_t *q, *r, *value;
    double timestamp;
    if (token) q = json_pack("{s:s,s:I}", "op", "fstat", "handle", (json_int_t)token);
    else q = json_pack("{s:s,s:s}", "op", "stat", "path", path);
    r = cm_rpc(h, q, NULL, 0, NULL, 0);
    if (!r) return -1;
    value = json_object_get(r, "result");
    st->st_ex_size = json_integer_value(json_object_get(value, "size"));
    st->st_ex_mode = json_integer_value(json_object_get(value, "mode"));
    st->st_ex_ino = json_integer_value(json_object_get(value, "inode"));
    st->st_ex_uid = json_integer_value(json_object_get(value, "uid"));
    st->st_ex_gid = json_integer_value(json_object_get(value, "gid"));
    st->st_ex_nlink = 1;
    st->st_ex_blocks = (st->st_ex_size + 511) / 512;
    timestamp = json_number_value(json_object_get(value, "mtime"));
    st->st_ex_mtime.tv_sec = timestamp;
    st->st_ex_mtime.tv_nsec = (timestamp - st->st_ex_mtime.tv_sec) * 1000000000;
    st->st_ex_ctime = st->st_ex_btime = st->st_ex_mtime;
    json_decref(r); return 0;
}

static int cm_stat(struct vfs_handle_struct *h, struct smb_filename *name)
{
    const char *path = cm_relative(h, name->base_name);
    if (!path || name->stream_name) { errno = EACCES; return -1; }
    if (SMB_VFS_NEXT_STAT(h, name) < 0) return -1;
    return cm_fill_stat(h, path, 0, &name->st);
}
static int cm_lstat(struct vfs_handle_struct *h, struct smb_filename *name) { return cm_stat(h, name); }
static int cm_fstat(struct vfs_handle_struct *h, struct files_struct *fsp, SMB_STRUCT_STAT *st)
{
    const char *path = cm_relative(h, fsp->fsp_name->base_name);
    if (!path) return -1;
    if (SMB_VFS_NEXT_FSTAT(h, fsp, st) < 0) return -1;
    return cm_fill_stat(h, path, cm_fh(h, fsp), st);
}
static int cm_fstatat(struct vfs_handle_struct *handle, const struct files_struct *dir,
                      const struct smb_filename *name, SMB_STRUCT_STAT *st, int flags)
{
    const char *path = cm_atpath(handle, dir, name);
    if (!path) return -1;
    if (SMB_VFS_NEXT_FSTATAT(handle, dir, name, st, flags) < 0) return -1;
    return cm_fill_stat(handle, path, 0, st);
}

static DIR *cm_fdopendir(struct vfs_handle_struct *h, struct files_struct *fsp, const char *mask, uint32_t attr)
{
    struct cm_directory *dir = calloc(1, sizeof(*dir));
    const char *path = cm_relative(h, fsp->fsp_name->base_name);
    if (!dir || !path) { free(dir); errno = ENOMEM; return NULL; }
    dir->path = strdup(path); dir->fd = fsp_get_io_fd(fsp);
    if (!dir->path) { free(dir); errno = ENOMEM; return NULL; }
    return (DIR *)dir;
}

static struct dirent *cm_readdir(struct vfs_handle_struct *h, struct files_struct *fsp, DIR *opaque)
{
    struct cm_directory *dir = (struct cm_directory *)opaque;
    const char *name;
    if (!dir->page || dir->index >= json_array_size(dir->page)) {
        json_t *response;
        json_decref(dir->page); dir->page = NULL;
        response = cm_rpc(h, json_pack("{s:s,s:s,s:I}", "op", "list", "path", dir->path,
                                      "offset", (json_int_t)dir->offset), NULL, 0, NULL, 0);
        if (!response) return NULL;
        dir->page = json_incref(json_object_get(response, "result")); dir->index = 0;
        json_decref(response);
        if (!json_is_array(dir->page)) { errno = EIO; return NULL; }
        if (!json_array_size(dir->page)) { errno = 0; return NULL; }
    }
    name = json_string_value(json_array_get(dir->page, dir->index++));
    if (!name || strlen(name) >= sizeof(dir->entry.d_name)) { errno = EIO; return NULL; }
    memset(&dir->entry, 0, sizeof(dir->entry));
    strlcpy(dir->entry.d_name, name, sizeof(dir->entry.d_name));
    dir->entry.d_type = DT_UNKNOWN; dir->offset++;
    return &dir->entry;
}

static void cm_rewinddir(struct vfs_handle_struct *h, DIR *opaque)
{
    struct cm_directory *dir = (struct cm_directory *)opaque;
    json_decref(dir->page); dir->page = NULL; dir->offset = dir->index = 0;
}

static int cm_closedir(struct vfs_handle_struct *h, DIR *opaque)
{
    struct cm_directory *dir = (struct cm_directory *)opaque;
    int result = close(dir->fd);
    free(dir->path); json_decref(dir->page); free(dir);
    return result;
}

static ssize_t cm_pread(struct vfs_handle_struct *h, struct files_struct *fsp, void *data, size_t n, off_t offset)
{
    json_t *r = cm_rpc(h, json_pack("{s:s,s:I,s:I,s:I}", "op", "read", "handle", (json_int_t)cm_fh(h, fsp),
                                  "count", (json_int_t)n, "offset", (json_int_t)offset), NULL, 0, data, n);
    ssize_t result;
    if (!r) return -1;
    result = json_integer_value(json_object_get(r, "result")); json_decref(r); return result;
}
static ssize_t cm_pwrite(struct vfs_handle_struct *h, struct files_struct *fsp, const void *data, size_t n, off_t offset)
{
    json_t *r = cm_rpc(h, json_pack("{s:s,s:I,s:I}", "op", "write", "handle", (json_int_t)cm_fh(h, fsp),
                                  "offset", (json_int_t)offset), data, n, NULL, 0);
    ssize_t result;
    if (!r) return -1;
    result = json_integer_value(json_object_get(r, "result")); json_decref(r); return result;
}

struct cm_io_state { ssize_t ret; struct vfs_aio_state aio; };
static struct tevent_req *cm_pread_send(struct vfs_handle_struct *h, TALLOC_CTX *mem,
        struct tevent_context *ev, struct files_struct *fsp, void *data, size_t n, off_t offset)
{
    struct tevent_req *req; struct cm_io_state *s;
    req = tevent_req_create(mem, &s, struct cm_io_state);
    if (!req) return NULL;
    s->ret = cm_pread(h, fsp, data, n, offset); s->aio.error = s->ret < 0 ? errno : 0;
    tevent_req_done(req); return tevent_req_post(req, ev);
}
static struct tevent_req *cm_pwrite_send(struct vfs_handle_struct *h, TALLOC_CTX *mem,
        struct tevent_context *ev, struct files_struct *fsp, const void *data, size_t n, off_t offset)
{
    struct tevent_req *req; struct cm_io_state *s;
    req = tevent_req_create(mem, &s, struct cm_io_state);
    if (!req) return NULL;
    s->ret = cm_pwrite(h, fsp, data, n, offset); s->aio.error = s->ret < 0 ? errno : 0;
    tevent_req_done(req); return tevent_req_post(req, ev);
}
static ssize_t cm_io_recv(struct tevent_req *req, struct vfs_aio_state *aio)
{
    struct cm_io_state *s = tevent_req_data(req, struct cm_io_state);
    *aio = s->aio; tevent_req_received(req); return s->ret;
}
static struct tevent_req *cm_fsync_send(struct vfs_handle_struct *h, TALLOC_CTX *mem,
        struct tevent_context *ev, struct files_struct *fsp)
{
    struct tevent_req *req; struct cm_io_state *s;
    req = tevent_req_create(mem, &s, struct cm_io_state);
    if (!req) return NULL;
    s->ret = cm_void(h, json_pack("{s:s,s:I}", "op", "sync", "handle", (json_int_t)cm_fh(h, fsp)));
    s->aio.error = s->ret < 0 ? errno : 0;
    tevent_req_done(req); return tevent_req_post(req, ev);
}
static int cm_fsync_recv(struct tevent_req *req, struct vfs_aio_state *aio) { return cm_io_recv(req, aio); }

static int cm_mkdirat(struct vfs_handle_struct *h, struct files_struct *dir, const struct smb_filename *name, mode_t mode)
{
    const char *path = cm_atpath(h, dir, name);
    if (!path) return -1;
    return cm_void(h, json_pack("{s:s,s:s}", "op", "mkdir", "path", path));
}
static int cm_unlinkat(struct vfs_handle_struct *h, struct files_struct *dir, const struct smb_filename *name, int flags)
{
    const char *path = cm_atpath(h, dir, name);
    if (!path) return -1;
    return cm_void(h, json_pack("{s:s,s:s,s:b}", "op", "unlink", "path", path, "directory", !!(flags & AT_REMOVEDIR)));
}
static int cm_renameat(struct vfs_handle_struct *h, struct files_struct *srcdir, const struct smb_filename *src,
        struct files_struct *dstdir, const struct smb_filename *dst, const struct vfs_rename_how *how)
{
    const char *source = cm_atpath(h, srcdir, src), *destination = cm_atpath(h, dstdir, dst);
    if (!source || !destination) return -1;
    return cm_void(h, json_pack("{s:s,s:s,s:s}", "op", "rename", "path", source, "destination", destination));
}
static int cm_ftruncate(struct vfs_handle_struct *h, struct files_struct *fsp, off_t length)
{
    return cm_void(h, json_pack("{s:s,s:I,s:I}", "op", "truncate", "handle", (json_int_t)cm_fh(h, fsp),
                               "length", (json_int_t)length));
}
static int cm_fallocate(struct vfs_handle_struct *h, struct files_struct *fsp, uint32_t mode, off_t off, off_t len)
{
    /* Samba's strict allocation is disabled. Refuse hole punching and every direct kernel write path. */
    errno = EOPNOTSUPP; return -1;
}
static ssize_t cm_sendfile(struct vfs_handle_struct *h, int fd, struct files_struct *fsp,
        const DATA_BLOB *header, off_t offset, size_t n) { errno = ENOSYS; return -1; }
static ssize_t cm_recvfile(struct vfs_handle_struct *h, int fd, struct files_struct *fsp, off_t offset, size_t n)
{ errno = ENOSYS; return -1; }

static off_t cm_lseek(struct vfs_handle_struct *h, struct files_struct *fsp, off_t offset, int whence)
{
    struct cm_file *file = VFS_FETCH_FSP_EXTENSION(h, fsp);
    SMB_STRUCT_STAT st;
    if (!file) { errno = EBADF; return -1; }
    if (whence == SEEK_END) { if (cm_fstat(h, fsp, &st) < 0) return -1; offset += st.st_ex_size; }
    else if (whence == SEEK_CUR) offset += file->position;
    else if (whence != SEEK_SET) { errno = EINVAL; return -1; }
    if (offset < 0) { errno = EINVAL; return -1; }
    file->position = offset; return offset;
}
static uint64_t cm_disk_free(struct vfs_handle_struct *h, struct files_struct *fsp, uint64_t *bsize, uint64_t *free, uint64_t *total)
{
    json_t *r = cm_rpc(h, json_pack("{s:s}", "op", "capacity"), NULL, 0, NULL, 0), *value;
    if (!r) return (uint64_t)-1;
    value = json_object_get(r, "result"); *bsize = 4096;
    *free = json_integer_value(json_object_get(value, "free")) / *bsize;
    *total = json_integer_value(json_object_get(value, "total")) / *bsize;
    json_decref(r); return *free * 4;
}

/* DOS timestamps/permissions are cosmetic. Real ownership and completion state are server-controlled. */
static int cm_fntimes(struct vfs_handle_struct *h, struct files_struct *fsp, struct smb_file_time *ft) { return 0; }
static int cm_fchmod(struct vfs_handle_struct *h, struct files_struct *fsp, mode_t mode) { return 0; }
static int cm_fchown(struct vfs_handle_struct *h, struct files_struct *fsp, uid_t uid, gid_t gid) { return 0; }
static int cm_mknodat(struct vfs_handle_struct *h, struct files_struct *dir,
        const struct smb_filename *name, mode_t mode, SMB_DEV_T dev) { errno = EACCES; return -1; }
static int cm_symlinkat(struct vfs_handle_struct *h, const struct smb_filename *contents,
        struct files_struct *dir, const struct smb_filename *name) { errno = EACCES; return -1; }
static int cm_linkat(struct vfs_handle_struct *h, struct files_struct *srcdir, const struct smb_filename *src,
        struct files_struct *dstdir, const struct smb_filename *dst, int flags) { errno = EACCES; return -1; }

static struct tevent_req *cm_offload_write_send(struct vfs_handle_struct *h, TALLOC_CTX *mem,
        struct tevent_context *ev, uint32_t fsctl, DATA_BLOB *token, off_t transfer_offset,
        struct files_struct *fsp, off_t dest_off, off_t n)
{
    struct tevent_req *req; struct cm_io_state *s;
    req = tevent_req_create(mem, &s, struct cm_io_state);
    if (!req) return NULL;
    tevent_req_nterror(req, NT_STATUS_NOT_SUPPORTED); return tevent_req_post(req, ev);
}
static NTSTATUS cm_offload_write_recv(struct vfs_handle_struct *h, struct tevent_req *req, off_t *copied)
{ *copied = 0; return NT_STATUS_NOT_SUPPORTED; }

static struct vfs_fn_pointers cm_functions = {
    .connect_fn = cm_connect, .disconnect_fn = cm_disconnect,
    .openat_fn = cm_openat, .close_fn = cm_close,
    .stat_fn = cm_stat, .lstat_fn = cm_lstat, .fstat_fn = cm_fstat, .fstatat_fn = cm_fstatat,
    .fdopendir_fn = cm_fdopendir, .readdir_fn = cm_readdir,
    .rewind_dir_fn = cm_rewinddir, .closedir_fn = cm_closedir,
    .pread_fn = cm_pread, .pwrite_fn = cm_pwrite,
    .pread_send_fn = cm_pread_send, .pread_recv_fn = cm_io_recv,
    .pwrite_send_fn = cm_pwrite_send, .pwrite_recv_fn = cm_io_recv,
    .fsync_send_fn = cm_fsync_send, .fsync_recv_fn = cm_fsync_recv,
    .mkdirat_fn = cm_mkdirat, .unlinkat_fn = cm_unlinkat, .renameat_fn = cm_renameat,
    .ftruncate_fn = cm_ftruncate, .fallocate_fn = cm_fallocate,
    .sendfile_fn = cm_sendfile, .recvfile_fn = cm_recvfile, .lseek_fn = cm_lseek,
    .disk_free_fn = cm_disk_free, .fntimes_fn = cm_fntimes, .fchmod_fn = cm_fchmod, .fchown_fn = cm_fchown,
    .mknodat_fn = cm_mknodat, .symlinkat_fn = cm_symlinkat, .linkat_fn = cm_linkat,
    .offload_write_send_fn = cm_offload_write_send, .offload_write_recv_fn = cm_offload_write_recv,
};

NTSTATUS vfs_cammon_init(TALLOC_CTX *ctx);
NTSTATUS vfs_cammon_init(TALLOC_CTX *ctx)
{ return smb_register_vfs(SMB_VFS_INTERFACE_VERSION, "cammon", &cm_functions); }
