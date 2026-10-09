"""Compile a narrow, version-checked CFFI bridge against the installed libnfs."""

from cffi import FFI

ffibuilder = FFI()
ffibuilder.cdef("""
void *cm_nfs_create(int version, int uid, int gid, int timeout, const char *client_id);
int cm_nfs_connect(void *, const char *);
const char *cm_nfs_error(void *);
void cm_nfs_destroy(void *);
int cm_nfs_open(void *, const char *, void **);
int cm_nfs_create_file(void *, const char *, void **);
int cm_nfs_close(void *, void *);
int cm_nfs_sync(void *, void *);
int64_t cm_nfs_read(void *, void *, uint64_t, uint64_t, void *);
int64_t cm_nfs_write(void *, void *, uint64_t, uint64_t, const void *);
int cm_nfs_size(void *, const char *, uint64_t *);
int cm_nfs_mkdir(void *, const char *);
int cm_nfs_rename(void *, const char *, const char *);
int cm_nfs_unlink(void *, const char *);
""")
ffibuilder.set_source("cammon._nfs", r"""
#include <nfsc/libnfs.h>
#include <fcntl.h>
#include <stdint.h>
#include <errno.h>
void *cm_nfs_create(int version, int uid, int gid, int timeout, const char *client_id) {
    struct nfs_context *c = nfs_init_context();
    if (!c) return NULL;
    if (nfs_set_version(c, version) < 0) { nfs_destroy_context(c); return NULL; }
    nfs_set_uid(c, uid); nfs_set_gid(c, gid);
    nfs_set_timeout(c, timeout * 1000);
    nfs_set_autoreconnect(c, 0);
    /* Independent reader/upload contexts must not replace each other's NFSv4 state. */
    if (version == 4) nfs4_set_client_name(c, client_id);
    return c;
}
int cm_nfs_connect(void *c, const char *address) {
    struct nfs_url *url = nfs_parse_url_full(c, address);
    if (!url) return -22;
    int r = nfs_mount(c, url->server, url->path);
    nfs_destroy_url(url);
    return r;
}
const char *cm_nfs_error(void *c) { return nfs_get_error(c); }
void cm_nfs_destroy(void *c) { nfs_destroy_context(c); }
int cm_nfs_open(void *c, const char *p, void **out) { return nfs_open(c, p, O_RDONLY, (struct nfsfh **)out); }
int cm_nfs_create_file(void *c, const char *p, void **out) {
    int r = nfs_open2(c, p, O_WRONLY|O_CREAT|O_TRUNC, 0660, (struct nfsfh **)out);
    /* Older libnfs NFSv4 CREATE implementations return EEXIST on upload retry. */
    if (r == -EEXIST) return nfs_open(c, p, O_WRONLY|O_TRUNC, (struct nfsfh **)out);
    return r;
}
int cm_nfs_close(void *c, void *f) { return nfs_close(c, f); }
int cm_nfs_sync(void *c, void *f) { return nfs_fsync(c, f); }
int64_t cm_nfs_read(void *c, void *f, uint64_t offset, uint64_t n, void *buf) {
#ifdef LIBNFS_API_V2
    return nfs_pread(c, f, buf, n, offset);
#else
    return nfs_pread(c, f, offset, n, buf);
#endif
}
int64_t cm_nfs_write(void *c, void *f, uint64_t offset, uint64_t n, const void *buf) {
#ifdef LIBNFS_API_V2
    return nfs_pwrite(c, f, buf, n, offset);
#else
    return nfs_pwrite(c, f, offset, n, buf);
#endif
}
int cm_nfs_size(void *c, const char *p, uint64_t *size) {
    struct nfs_stat_64 st;
    int r = nfs_stat64(c, p, &st);
    if (!r) *size = st.nfs_size;
    return r;
}
int cm_nfs_mkdir(void *c, const char *p) { return nfs_mkdir(c, p); }
int cm_nfs_rename(void *c, const char *a, const char *b) { return nfs_rename(c, a, b); }
int cm_nfs_unlink(void *c, const char *p) { return nfs_unlink(c, p); }
""", libraries=["nfs"])

if __name__ == "__main__":
    ffibuilder.compile(verbose=True)
