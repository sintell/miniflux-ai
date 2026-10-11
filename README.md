# miniflux-ai
Miniflux with AI

This project integrates with Miniflux to fetch RSS feed content via API or webhook. It then utilizes large language models (e.g., Ollama, ChatGPT, LLaMA, Gemini) to generate summaries, translations, and AI-driven news insights.

## Features

- **Personal digest**: Configurable output language and editorial length, validated
  article links, merged source coverage, optional forecast-aware creative openings,
  and persistent SQLite-backed editions.
- **Deployment guide**: [Configuration and behavior](docs/personal-digest.md) ·
  [NAS/Portainer migration, Docker Hub publishing, and rollback](docs/nas-deployment.md).

- **Miniflux Integration**: Seamlessly fetch unread entries from Miniflux or trigger via webhook.
- **Native metadata enrichment**: Optional fork integration stores relevance, summaries,
  and topics without changing publisher content. See [metadata configuration and rollout](docs/metadata.md).
- **Schedule Interval**: Specifies the time interval for requesting the miniflux api.
- **LLM Processing**: Generate summaries, translations, etc. based on your chosen LLM agent.
- **AI News**: Use the LLM agent to generate AI morning and evening news from feed content.
- **Flexible Configuration**: Easily modify or add new agents via the `config.yml` file.
- **Markdown and HTML Support**: Outputs in Markdown or styled HTML blocks, depending on configuration.

<table>
  <tr>
    <td>
      summaries, translations
    </td>
    <td>
      AI News
    </td> 
  </tr>
  <tr>
    <td> 
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/11c208d9-816a-4c8c-bc00-2f780529e58d">
        <source media="(prefers-color-scheme: light)" srcset="https://github.com/user-attachments/assets/c97e2774-ec10-4acb-bef7-25cf8d43da15">
        <img alt="miniflux AI summaries translations" src="https://github.com/user-attachments/assets/c97e2774-ec10-4acb-bef7-25cf8d43da15" width="400" > 
      </picture>
    </td>
    <td> 
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/b40f5bdd-d265-4beb-a14c-d39d6624760b">
        <source media="(prefers-color-scheme: light)" srcset="https://github.com/user-attachments/assets/e5985025-15f3-43b0-982b-422575962783">
        <img alt="miniflux AI summaries translations" src="https://github.com/user-attachments/assets/e5985025-15f3-43b0-982b-422575962783" width="400" > 
      </picture>
    </td>
  </tr>
</table>

## Requirements

- Python 3.11+
- Dependencies: Install via `pip install -r requirements.txt`
- Miniflux API Key
- API Key compatible with OpenAI-compatible LLMs (e.g., Ollama for LLaMA 3.1)

## Configuration

Copy `config.sample.English.yml` or `config.sample.Chinese.yml` to `config.yml` and
adapt the existing settings. YAML values support `${ENV_VAR}` and
`${ENV_VAR:-default}`. Supply Miniflux and LLM API keys separately through the
container environment; do not commit real credentials.

> If using a webhook, enter the URL in Settings > Integrations > Webhook > Webhook URL.
> 
> If deploying in a container alongside Miniflux, use the following URL:
> http://miniflux_ai/api/miniflux-ai.

- **Miniflux**: Base URL and API key.
- **LLM**: Model, endpoint, per-attempt timeout, shared concurrent-request limit,
  request-per-minute budget and bounded retries.
- **AI News**: Language, timezone, schedule, editorial limits, prompts and optional weather.
- **Agents**: Define prompts and allow/deny filters; `style_block` wraps generated
  text in a blockquote. The summary agent uses `ai_news.prompts.article`.
  Summaries use a short overview and concise topic bullets, or short paragraphs for
  simple articles. Legacy summaries render basic Markdown, including bold labels;
  metadata summaries use spaced plain-text bullets supported by the Miniflux display.


## Docker Setup

The existing `docker-compose.yml` runs the AI application on the external
`reverse_proxy` network alongside your existing Miniflux and LLM gateway. Set
`MINIFLUX_AI_IMAGE` to the fork's versioned Docker Hub image and
`MINIFLUX_AI_DIRECTORY` to the directory containing `config.yml` and `data/`.

`config.yml` is mounted read-only. Mount the whole `data/` directory for SQLite,
locks and `feeds_status.json`. Before upgrading an existing deployment, follow the
[backup and JSON migration steps](docs/nas-deployment.md); do not recreate the old
container before recovering its unmounted state files. `FLASK_PORT` defaults to 80
and is configurable through the environment.

After configuring credentials, timezone and image, start a shell-managed stack:

```bash
sudo docker compose up -d
```

## Usage

1. Ensure `config.yml` is properly configured.
2. Run the script: `python main.py`
3. The script fetches unread RSS entries and processes them with the LLM. Explicit
   `legacy_content` mode updates content; `metadata` mode writes producer metadata
   and requires the metadata-enabled Miniflux fork plus your personal rating criteria.

## Roadmap
- [x] Add daily summary(by title, Summary of existing AI)
  - [x] Add Morning and Evening News（e.g. 9/24: AI Morning News, 9/24: AI Evening News）
  - [x] Add timed summary

## FAQ
<details>
<summary> If the formatting of summary content is incorrect, add the following code in Settings > Custom CSS: </summary>

```
pre code {
    white-space: pre-wrap;
    word-wrap: break-word;
}
```
</details>

<details>
<summary> fetcher: refusing to access private network host "xx.xx.xx.xx" </summary>

Starting with Miniflux 2.2.18, `FETCHER_ALLOW_PRIVATE_NETWORKS=1` must now be enabled to access feeds hosted on a local network. See the [official release notes](https://github.com/miniflux/v2/releases/tag/2.2.18) for details.

</details>

<details>
<summary> level=WARN msg="Unable to send new entries to Webhook" ... client: connection to private network is blocked: host "miniflux-ai" resolves to a non-public IP address" </summary>
  
Starting with Miniflux 2.2.18, `INTEGRATION_ALLOW_PRIVATE_NETWORKS=1` must now be enabled to access third-party integration services hosted on a local network. See the [official release notes](https://github.com/miniflux/v2/releases/tag/2.2.18) for details.

</details>

## Contributing

Feel free to fork this repository and submit pull requests. Contributions and issues are welcome!

<a href="https://github.com/Qetesh/miniflux-ai/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=Qetesh/miniflux-ai" />
</a>


## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=Qetesh/miniflux-ai&type=Date)](https://star-history.com/#Qetesh/miniflux-ai&Date)

## License

This project is licensed under the MIT License.
