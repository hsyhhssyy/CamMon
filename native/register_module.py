"""Install CamMon's source and build declaration in a pinned Samba source checkout."""
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1])
project = Path(__file__).resolve().parent
shutil.copy2(project / "vfs_cammon.c", root / "source3/modules/vfs_cammon.c")
path = root / "source3/modules/wscript_build"
text = path.read_text()
text = text.replace("deps='samba-util jansson', init_function='', internal_module=False,",
                    "deps='samba-util jansson', init_function='vfs_cammon_init', internal_module=False,")
if "SAMBA3_MODULE('vfs_cammon'" not in text:
    text += """
bld.SAMBA3_MODULE('vfs_cammon', subsystem='vfs', source='vfs_cammon.c',
    deps='samba-util jansson', init_function='vfs_cammon_init', internal_module=False,
    enabled=True)
"""
path.write_text(text)
