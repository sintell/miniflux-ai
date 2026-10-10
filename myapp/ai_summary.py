from flask import request, abort, jsonify
import hmac
import hashlib
import concurrent.futures
from common.logger import logger

from services.miniflux_client import create_client, MetadataAPIError
from services.editorial import GenerationError
from common.config import Config
from core import process_entry
from myapp import app

@app.route('/api/miniflux-ai', methods=['POST'])
def miniflux_ai():
    """miniflux Webhook API
    publish new feed entries to this API endpoint
    ---
    post:
      description: Create a random pet
      parameters:
        - in: body
          name: body
          required: True
      responses:
        200:
          content:
            application/json:
              status: string
    """
    config = Config()
    miniflux_client = create_client(config)
    webhook_secret = config.miniflux_webhook_secret
    if not webhook_secret:
        abort(403)

    if request.method == 'POST':
        payload = request.get_data()
        signature = request.headers.get('X-Miniflux-Signature', '')
        hmac_signature = hmac.new(webhook_secret.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(hmac_signature, signature):
            abort(403)  # 返回403 Forbidden
        entries = request.json
        logger.info('Get unread entries via webhook: ' + str(len(entries['entries'])))
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=config.llm_max_workers) as executor:
            futures = []
            for i in entries['entries']:
                i['feed'] = entries['feed']
                futures.append(executor.submit(process_entry, miniflux_client, i, config))
            
            for future in concurrent.futures.as_completed(futures):
                try:
                    data = future.result()
                except Exception as e:
                    reason = str(e) if isinstance(e, (MetadataAPIError, GenerationError)) else type(e).__name__
                    logger.error('Webhook enrichment failed: %s', reason)
                    return jsonify({'status': 'enrichment_failed'}), 500

        return jsonify({'status': 'ok'})
