FROM python:3.12-slim

# git is needed for `review --base <ref>` and for `mine`.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Mount the repository under review at /repo:
#   docker run --rm -v "$PWD:/repo" -e LLM_PROXY_API_KEY -e GITLAB_TOKEN -e CI_PROJECT_ID -e CI_MERGE_REQUEST_IID \
#       review-agent review --base main --post gitlab --fail-on-critical
WORKDIR /repo
ENTRYPOINT ["review-agent"]
CMD ["--help"]
