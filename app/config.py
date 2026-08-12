"""
Configuration settings for Local Color Service FastAPI App.
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic import ConfigDict
from pydantic_settings import BaseSettings

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Most runtime modules deliberately read environment variables directly (FFmpeg,
# model adapters, face protection, etc.).  Pydantic reads `.env` for Settings,
# but does not export those values into os.environ; load it once so both access
# paths observe the same configuration.  Real process environment still wins.
load_dotenv(PROJECT_ROOT / ".env", override=False)


class Settings(BaseSettings):
    PROJECT_NAME: str = "Local Color Service"
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/v1"
    
    DATA_DIR: Path = PROJECT_ROOT / "data"
    DATABASE_URL: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'local_color.db'}"
    
    FFMPEG_PATH: str = r"C:\ffmpeg_cuda\bin\ffmpeg.exe"
    FFPROBE_PATH: str = r"C:\ffmpeg_cuda\bin\ffprobe.exe"
    
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    model_config = ConfigDict(env_file=".env", extra="ignore")


settings = Settings()
