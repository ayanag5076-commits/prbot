# Minimal, offline test image used by Agent C (the tester).
# Build once:  docker build -t prbot-tester:py3.12 -f docker/tester.Dockerfile .
# Containers run with --network none, so bake any test deps your target repo needs in here.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir "pytest>=8"
WORKDIR /workspace
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider"]
