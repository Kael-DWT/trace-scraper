import asyncio
import hashlib
import logging
import re
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import STANDARD_FIELDS
from app.rule_engine import engine

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger('trace')

app = FastAPI(title='Trace Source Universal Extractor', version='1.0')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

class QueryRequest(BaseModel):
    url: str

@app.on_event('startup')
async def startup():
    logger.info('initializing shared browser...')
    await asyncio.to_thread(engine.init_browser)
    logger.info('browser ready')

@app.on_event('shutdown')
async def shutdown():
    await asyncio.to_thread(engine.close_browser)
    logger.info('browser closed')

@app.get('/api/health')
def health() -> dict[str, Any]:
    return {'status': 'ok', 'browser': engine._browser is not None}

@app.post('/api/query')
async def query(req: QueryRequest) -> dict[str, Any]:
    from app.fetcher import normalize_url
    url = normalize_url(req.url)
    logger.info('query %s', url[:80])
    try:
        raw = await asyncio.to_thread(engine.query, url)
        source = raw.get('source', 'live')
        fields = raw.get('fields', {})
        elapsed = raw.get('elapsed_ms', 0)

        gn = fields.get('goods_name', '')
        cn = fields.get('company_name', '')
        out: dict[str, Any] = {f: fields.get(f, '') for f in STANDARD_FIELDS}
        raw_key = f'{url}{gn}{cn}'
        out['goods_code'] = hashlib.md5(raw_key.encode('utf-8')).hexdigest()
        out['trace_website'] = url
        out['query_count'] = int(re.sub(r'\D', '', str(out.get('query_count', ''))) or 0)

        ok = bool(gn or cn)
        return {'success': ok, 'data': {'source': source, 'fields': out, 'elapsed_ms': elapsed}}
    except Exception as exc:
        logger.exception('query failed')
        return {'success': False, 'data': {'source': 'error', 'fields': {}, 'elapsed_ms': 0}}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8000)