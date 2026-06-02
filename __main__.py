from .main import create_app
import os
from aiohttp import web

web.run_app(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
