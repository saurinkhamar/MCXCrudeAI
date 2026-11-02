"""Convenience entry point for the local assistant API on loopback port 8001."""
import uvicorn
uvicorn.run('mcx_ai.main:app',host='127.0.0.1',port=8001,reload=True)
