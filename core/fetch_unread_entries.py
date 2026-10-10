import concurrent.futures
import time

from common.logger import logger
from core.process_entries import process_entry
from services.editorial import GenerationError
from services.miniflux_client import MetadataAPIError, OUTPUT_FIELDS
from services.state import State


def fetch_unread_entries(config, miniflux_client):
    metadata = getattr(config, 'enrichment_mode', 'legacy_content') == 'metadata'
    projection = {}
    pending = []
    if metadata:
        # Fail before fan-out or any LLM call if the fork/registry is unavailable.
        miniflux_client.ensure_metadata(config.metadata_input_fields, force=True)
        projection = {'meta': list(dict.fromkeys(OUTPUT_FIELDS + config.metadata_input_fields))}
        pending = State(config.state_path).pending_enrichments()
    start_time = time.time()
    seen = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.llm_max_workers) as executor:
        def process_batch(batch):
            futures = [executor.submit(process_entry, miniflux_client, entry, config) for entry in batch]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, (MetadataAPIError, GenerationError)) else type(exc).__name__
                    logger.error('Article processing failed: %s', reason)

        # Persisted work is independent of unread status or changed_at.
        process_batch([{'id': entry_id} for entry_id in pending])
        seen.update(pending)
        offset = 0
        while True:
            entries = miniflux_client.get_entries(status=['unread'], limit=1000, offset=offset,
                                                  order='id', direction='asc', **projection)
            batch = entries['entries']
            logger.info('Unread discovery: offset=%d entries=%d', offset, len(batch))
            process_batch([entry for entry in batch if entry['id'] not in seen])
            seen.update(entry['id'] for entry in batch)
            offset += len(batch)
            if not batch or offset >= entries['total']:
                break

    if seen and time.time() - start_time >= 3:
        logger.info('Done')
