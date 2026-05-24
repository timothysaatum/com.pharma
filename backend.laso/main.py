import logging
import logging.config

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.openapi.utils import get_openapi
from pydantic import ValidationError
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import engine
from app.api.v1 import router as v1_router
from app.middleware.rate_limit import RateLimitMiddleware


# ============================================================================
# CONFIGURATION & SETTINGS
# ============================================================================

settings = get_settings()

# Configure structured logging
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "detailed": {
            "format": "%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": "INFO",
            "formatter": "default",
            "stream": "ext://sys.stdout",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": "DEBUG",
            "formatter": "detailed",
            "filename": "logs/app.log",
            "maxBytes": 10485760,  # 10MB
            "backupCount": 10,
        },
        "error_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": "ERROR",
            "formatter": "detailed",
            "filename": "logs/error.log",
            "maxBytes": 10485760,  # 10MB
            "backupCount": 10,
        },
    },
    "loggers": {
        "": {  # root logger
            "handlers": ["console", "file"] if settings.ENVIRONMENT == "production" else ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "app": {
            "handlers": ["console", "file", "error_file"] if settings.ENVIRONMENT == "production" else ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
        "uvicorn.access": {
            "handlers": ["console", "file"] if settings.ENVIRONMENT == "production" else ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)


# ============================================================================
# LIFECYCLE EVENTS
# ============================================================================

async def lifespan(app: FastAPI):
    """
    Manage application lifecycle - startup and shutdown events.
    """
    # Startup
    logger.info(f"Starting {settings.PROJECT_NAME} v{settings.VERSION}")
    logger.info(f"Environment: {settings.ENVIRONMENT}")
    logger.info(f"Database: {settings.DATABASE_URL}")
    
    try:
        from app.db.base import Base
        
        # Database setup
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
            logger.info("Database connection established")
            await conn.run_sync(Base.metadata.create_all)
            logger.info("Database tables created/verified")
        
    except Exception as e:
        logger.error(f"Database connection failed: {str(e)}")
        raise
    
    try:
        from app.utils.notifications import setup_notifications, EmailConfig, ArkeselConfig
        
        # Email config (optional)
        email_config = None
        if settings.SMTP_HOST and settings.SMTP_USER:
            email_config = EmailConfig(
                smtp_host=settings.SMTP_HOST,
                smtp_port=settings.SMTP_PORT,
                smtp_user=settings.SMTP_USER,
                smtp_password=settings.SMTP_PASSWORD,
                from_email=settings.FROM_EMAIL,
                from_name=settings.PROJECT_NAME
            )
            logger.info("Email notifications configured")
        
        # Arkesel SMS config
        arkesel_config = None
        if settings.ARKESEL_API_KEY:
            arkesel_config = ArkeselConfig(
                api_key=settings.ARKESEL_API_KEY,
                sender_id=settings.ARKESEL_SENDER_ID,
                base_url=settings.ARKESEL_BASE_URL
            )
            logger.info("Arkesel SMS configured")
        
        # Initialize
        setup_notifications(
            email_config=email_config,
            arkesel_config=arkesel_config
        )
        logger.info("Notification system initialized")
        
    except Exception as e:
        logger.warning(f"Notification setup failed (non-critical): {str(e)}")
    
    yield
    
    # Shutdown
    logger.info("Shutting down application")
    await engine.dispose()
    logger.info("All resources cleaned up")


# ============================================================================
# FASTAPI APP INITIALIZATION
# ============================================================================

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="Comprehensive pharmacy management system with inventory, sales, and prescription management",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


# ============================================================================
# MIDDLEWARE CONFIGURATION
# ============================================================================

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
        "http://tauri.localhost",
        "tauri://localhost",
        "https://tauri.localhost",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if settings.RATE_LIMIT_ENABLED:
    app.add_middleware(RateLimitMiddleware)

# ============================================================================
# CUSTOM MIDDLEWARE
# ============================================================================

@app.middleware("http")
async def add_request_id_middleware(request: Request, call_next):
    """
    Add request ID to all requests for tracing and logging.
    """
    import uuid
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.middleware("http")
async def log_requests_middleware(request: Request, call_next):
    """
    Log all HTTP requests and responses.
    """
    import time
    start_time = time.time()
    
    response = await call_next(request)
    
    process_time = time.time() - start_time
    request_id = getattr(request.state, "request_id", "unknown")
    
    logger.info(
        f"[{request_id}] {request.method} {request.url.path} - "
        f"Status: {response.status_code} - Duration: {process_time:.3f}s"
    )
    
    response.headers["X-Process-Time"] = str(process_time)
    return response


# ============================================================================
# EXCEPTION HANDLERS
# ============================================================================

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Handle validation errors with detailed error information.
    Properly serializes all error data including ValueError objects.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    
    logger.warning(
        f"[{request_id}] Validation error on {request.method} {request.url.path}"
    )
    
    # Convert errors to JSON-serializable format
    formatted_errors = []
    for error in exc.errors():
        # Create a clean error dict with only serializable data
        clean_error = {
            "loc": list(error.get("loc", [])),
            "msg": str(error.get("msg", "")),
            "type": error.get("type", "validation_error"),
        }
        
        # Safely handle the input field
        if "input" in error:
            input_val = error["input"]
            # Convert complex objects to string representation
            if hasattr(input_val, '__dict__'):
                clean_error["input"] = f"<{type(input_val).__name__} object>"
            else:
                try:
                    # Try to convert to JSON-safe format
                    import json
                    json.dumps(input_val)  # Test if serializable
                    clean_error["input"] = input_val
                except (TypeError, ValueError):
                    clean_error["input"] = str(input_val)
        
        # Safely handle context
        if "ctx" in error:
            try:
                # Convert all context values to strings to avoid serialization errors
                clean_error["ctx"] = {
                    k: str(v) for k, v in error["ctx"].items()
                }
            except:
                pass
        
        formatted_errors.append(clean_error)
    
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Validation error",
            "errors": formatted_errors,
            "request_id": request_id,
        },
    )


@app.exception_handler(ValidationError)
async def pydantic_validation_exception_handler(request: Request, exc: ValidationError):
    """
    Handle Pydantic ValidationError (from model validation).
    """
    request_id = getattr(request.state, "request_id", "unknown")
    
    logger.warning(
        f"[{request_id}] Pydantic validation error on {request.method} {request.url.path}"
    )
    
    # Convert errors to JSON-serializable format
    formatted_errors = []
    for error in exc.errors():
        formatted_errors.append({
            "loc": list(error.get("loc", [])),
            "msg": str(error.get("msg", "")),
            "type": error.get("type", "validation_error"),
        })
    
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Validation error",
            "errors": formatted_errors,
            "request_id": request_id,
        },
    )


@app.exception_handler(ValueError)
async def value_error_exception_handler(request: Request, exc: ValueError):
    """
    Handle ValueError (e.g., from field validators).
    """
    request_id = getattr(request.state, "request_id", "unknown")
    
    logger.warning(
        f"[{request_id}] ValueError on {request.method} {request.url.path}: {str(exc)}"
    )
    
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "detail": str(exc),
            "request_id": request_id,
            "type": "ValueError"
        },
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """
    Handle unexpected exceptions with proper logging.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    
    logger.error(
        f"[{request_id}] Unhandled exception on {request.method} {request.url.path}: {str(exc)}",
        exc_info=True,
    )
    
    error_response = {
        "detail": "Internal server error" if settings.ENVIRONMENT == "production" else str(exc),
        "request_id": request_id,
    }
    
    if settings.ENVIRONMENT != "production":
        error_response["type"] = type(exc).__name__
    
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_response,
    )


# ============================================================================
# ROUTES & ENDPOINTS
# ============================================================================

# API v1 routes
app.include_router(
    v1_router,
    prefix=settings.API_PREFIX,
)


# ============================================================================
# HEALTH CHECK ENDPOINTS
# ============================================================================

@app.get("/health", tags=["System"])
async def health_check():
    """
    Basic health check endpoint.
    Returns 200 OK if the service is running.
    """
    return {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT,
    }


@app.get("/health/deep", tags=["System"])
async def deep_health_check():
    """
    Deep health check including database connectivity.
    """
    health_status = {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT,
        "checks": {
            "database": "unknown",
        },
    }
    
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        health_status["checks"]["database"] = "healthy"
    except Exception as e:
        logger.error(f"Database health check failed: {str(e)}")
        health_status["status"] = "degraded"
        health_status["checks"]["database"] = f"unhealthy: {str(e)}"
    
    return health_status


# ============================================================================
# OPENAPI CUSTOMIZATION
# ============================================================================

def custom_openapi():
    """
    Customize OpenAPI schema with additional metadata.
    """
    if app.openapi_schema:
        return app.openapi_schema
    
    openapi_schema = get_openapi(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        description=app.description,
        routes=app.routes,
    )
    
    openapi_schema["info"]["x-logo"] = {
        "url": "https://fastapi.tiangolo.com/img/logo-margin/logo-teal.png"
    }
    
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


# ============================================================================
# ROOT ENDPOINT
# ============================================================================

@app.get("/", tags=["System"])
async def root():
    """
    Root endpoint providing API information.
    """
    return {
        "message": f"Welcome to {settings.PROJECT_NAME}",
        "version": settings.VERSION,
        "docs": f"{settings.API_PREFIX or '/api/v1'}/docs",
        "redoc": f"{settings.API_PREFIX or '/api/v1'}/redoc",
        "health": "/health",
        "deep_health": "/health/deep",
    }


# ============================================================================
# APPLICATION ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    
    # Determine host and port
    host = "0.0.0.0" if settings.ENVIRONMENT == "production" else "127.0.0.1"
    port = 8000
    
    # Determine reload behavior
    reload = settings.ENVIRONMENT != "production"
    
    # Start server
    logger.info(f"Starting server on {host}:{port}")
    
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info" if settings.ENVIRONMENT == "production" else "debug",
        access_log=True,
        workers=1 if reload else 4,
    )