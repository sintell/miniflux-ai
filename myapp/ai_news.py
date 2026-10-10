from flask import Response
from common.config import Config
from services.rss import render_feed
from services.state import State
from myapp import app


@app.route('/rss/ai-news', methods=['GET'])
def miniflux_ai_news():
    config = Config()
    return Response(render_feed(State(config.state_path).digests(), config.ai_news_url),
                    content_type='application/rss+xml; charset=utf-8')
