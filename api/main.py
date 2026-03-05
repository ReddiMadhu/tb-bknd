"""FastAPI application for Excel Relationship Discovery"""
import sys
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from loguru import logger

from api.config import config
from storage.database import init_database

# ─── Logging Configuration ────────────────────────────────────────────────────
# Change LOG_LEVEL to "WARNING" or "INFO" to see more output
LOG_LEVEL = "ERROR"

# 1. Replace loguru's default DEBUG sink with ERROR-only
logger.remove()
logger.add(
    sys.stderr,
    level=LOG_LEVEL,
    format="<red>{time:HH:mm:ss}</red> | <level>{level: <8}</level> | "
           "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    colorize=True,
)

# 2. Intercept standard `logging` (uvicorn, FastAPI, SQLAlchemy) → loguru
class _InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

logging.basicConfig(handlers=[_InterceptHandler()], level=logging.ERROR, force=True)
for noisy_logger in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi"):
    logging.getLogger(noisy_logger).handlers = [_InterceptHandler()]
    logging.getLogger(noisy_logger).propagate = False
# ──────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan events for FastAPI application"""
    # Startup
    logger.info("Starting BI Migration & Discovery API...")

    # Ensure directories exist
    config.ensure_directories()

    # Initialize database
    init_database()

    logger.info(f"API started on {config.API_HOST}:{config.API_PORT}")

    yield

    # Shutdown
    logger.info("Shutting down API...")


# Create FastAPI application
app = FastAPI(
    title=config.API_TITLE,
    version=config.API_VERSION,
    description=config.API_DESCRIPTION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=config.CORS_ALLOW_CREDENTIALS,
    allow_methods=config.CORS_ALLOW_METHODS,
    allow_headers=config.CORS_ALLOW_HEADERS,
)


@app.get("/", tags=["root"])
async def root():
    """Root endpoint"""
    return {
        "message": "BI Migration & Discovery API",
        "version": config.API_VERSION,
        "features": ["Excel Relationship Discovery", "Tableau to Power BI Migration"],
        "docs": "/docs",
        "health": "/health"
    }


@app.get("/health", tags=["health"])
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "excel-relationship-discovery-api",
        "version": config.API_VERSION
    }


# Import and include routers
from api.routers import jobs, websocket, migration, workbook_metadata

app.include_router(
    jobs.router,
    prefix=f"{config.API_PREFIX}/jobs",
    tags=["jobs"]
)

app.include_router(
    websocket.router,
    prefix=f"{config.API_PREFIX}",
    tags=["websocket"]
)

app.include_router(
    migration.router,
    tags=["migration"]
)

app.include_router(
    workbook_metadata.router,
    tags=["workbook-metadata"]
)


# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Handle uncaught exceptions"""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": "An unexpected error occurred",
                "details": str(exc) if config.API_HOST == "0.0.0.0" else None  # Only in dev
            }
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        host=config.API_HOST,
        port=config.API_PORT,
        reload=True,
        log_level="info"
    )
