from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH)
data = [(str(root / '报销清单表.xlsx'), '.')]
data += collect_data_files('rapidocr')
hidden = collect_submodules('rapidocr.inference_engine.onnxruntime')
a = Analysis(
    [str(root / 'invoice_app.py')],
    pathex=[str(root)],
    binaries=[], datas=data, hiddenimports=hidden,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['torch', 'tensorflow', 'paddle', 'openvino', 'tensorrt', 'matplotlib', 'pandas', 'pytest', 'IPython'],
    noarchive=False,
)

# RapidOCR accepts an in-memory NumPy image in this application.  Its optional
# video codecs and uncommon Pillow format backends are therefore unnecessary.
# Some wheel DLLs are also discovered twice: once beside the executable and
# once in the wheel's private DLL directory.  Keep the private copy used by the
# package and remove only the duplicate at the archive root.
optional_binaries = {'cv2/opencv_videoio_ffmpeg500_64.dll'}
optional_binary_prefixes = (
    'PIL/_avif.', 'PIL/_imagingcms.', 'PIL/_imagingft.',
    'PIL/_imagingmath.', 'PIL/_imagingtk.', 'PIL/_webp.',
)
duplicate_root_prefixes = (
    'geos-', 'geos_c-', 'libscipy_openblas64_',
    'msvcp140-90bc', 'msvcp140-a4c',
)

def keep_binary(entry):
    destination = entry[0].replace('\\', '/')
    if destination in optional_binaries or destination.startswith(optional_binary_prefixes):
        return False
    if '/' not in destination and destination.startswith(duplicate_root_prefixes):
        return False
    return True

filtered_binaries = [entry for entry in a.binaries if keep_binary(entry)]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='发票报销工具',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False)
bundle = COLLECT(exe, filtered_binaries, a.datas,
                 strip=False, upx=False, name='发票报销工具')
