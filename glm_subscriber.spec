# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from pathlib import Path

block_cipher = None

project_root = os.path.abspath('.')

a = Analysis(
    ['glm_subscriber/__main__.py'],
    pathex=[project_root],
    binaries=[],
    datas=[
        ('js/ycl.js', 'js'),
        ('config.yaml', '.'),
        (r'E:\Python311\Lib\site-packages\rapidocr\default_models.yaml', 'rapidocr'),
        (r'E:\Python311\Lib\site-packages\rapidocr\config.yaml', 'rapidocr'),
        (r'E:\Python311\Lib\site-packages\rapidocr\models', 'rapidocr/models'),
        (r'E:\Python311\Lib\site-packages\rapidocr_onnxruntime\config.yaml', 'rapidocr_onnxruntime'),
        (r'E:\Python311\Lib\site-packages\rapidocr_onnxruntime\models', 'rapidocr_onnxruntime/models'),
    ],
    hiddenimports=[
        'glm_subscriber',
        'glm_subscriber.main',
        'glm_subscriber.browser',
        'glm_subscriber.captcha_solver',
        'glm_subscriber.captcha_capture',
        'glm_subscriber.rapidocr_engine',
        'glm_subscriber.notify',
        'glm_subscriber.types',
        'rapidocr_onnxruntime',
        'onnxruntime',
        'shapely',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'paddleocr',
        'paddlepaddle',
        'paddle',
        'paddlex',
        'tensorflow',
        'torch',
        'matplotlib',
        'scipy',
        'tkinter',
        'pandas',
        'pytest',
        'py',
        'pygments',
        'rich',
        'lxml',
        'openpyxl',
        'jinja2',
        'pythoncom',
        'win32com',
        'Pythonwin',
        'pywintypes',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='glm-subscriber',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='glm-subscriber',
)
