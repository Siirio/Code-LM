# PyInstaller spec for CodeLM backend
# Run from backend/ directory: pyinstaller backend.spec
import os

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        # Include built React frontend
        ('static', 'static'),
        # Include all Python sub-packages as data (they're imported dynamically)
        ('api', 'api'),
        ('orchestrator', 'orchestrator'),
        ('scanner', 'scanner'),
        ('storage', 'storage'),
        ('llm', 'llm'),
        ('config.py', '.'),
        ('embedding.py', '.'),
        # ONNX model files — generated once by scripts/setup_embedding_model.py
        ('models', 'models'),
    ],
    hiddenimports=[
        # uvicorn internals (not auto-detected)
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        # SQLAlchemy async
        'sqlalchemy.dialects.postgresql',
        'sqlalchemy.dialects.postgresql.asyncpg',
        'sqlalchemy.ext.asyncio',
        'asyncpg',
        'asyncpg.pgproto',
        'asyncpg.pgproto.pgproto',
        # Pydantic v2
        'pydantic.deprecated.class_validators',
        'pydantic_core',
        # FastAPI
        'fastapi.staticfiles',
        'fastapi.responses',
        # LLM providers
        'anthropic',
        'openai',
        'llm.deepseek_provider',
        # Embedding (ONNX runtime — no PyTorch)
        'onnxruntime',
        'tokenizers',
        # Storage clients
        'qdrant_client',
        'qdrant_client.async_qdrant_client',
        'neo4j',
        'neo4j.exceptions',
        # Utilities
        'dotenv',
        'aiofiles',
        'httpx',
        # Windows PTY support
        'winpty',
        # Scanner sub-modules (static analysis misses dynamically-imported files)
        'scanner.project_scanner',
        'scanner.import_resolver',
        'scanner.validator',
        'scanner.role_inference',
        'scanner.java_treesitter',
        'scanner.module_detector',
        # Orchestrator sub-modules
        'orchestrator.hypothesis_engine',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'pandas', 'PIL', 'tkinter',
        'torch', 'torchvision', 'torchaudio', 'sentence_transformers',
        'optimum',
        'test', 'tests', 'unittest',
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
    name='codelm-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,   # No console window on Windows
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='codelm-backend',
)
