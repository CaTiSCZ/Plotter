import PyInstaller.__main__

from scada import APPLICATION_VERSION

PyInstaller.__main__.run([
    'scada.py',
    '--onefile',
    '--name', f"scada_v{APPLICATION_VERSION}",
    '--specpath', 'build'
])
