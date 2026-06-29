import PyInstaller.__main__

from scada import APPLICATION_VERSION

PyInstaller.__main__.run([
    'scada.py',
    '--onefile',
    '--name', f"scada_v{APPLICATION_VERSION}",
    '--specpath', 'build',
    '--collect-submodules', 'crcmod',  # bundle crcmod incl. its C ext (fast CRC); else exe falls back to slow pure-Python CRC
])
