FROM python:3.12-slim AS base
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir pip==26.2.2 && pip install --no-cache-dir -r requirements.txt
COPY . .

# Hard test gate: the final stage copies the marker from this stage, so a
# plain `docker compose build` cannot succeed with failing tests.
FROM base AS test
RUN pip install --no-cache-dir pytest==8.3.4 && python -m pytest -q && touch /tests-passed

FROM base AS final
COPY --from=test /tests-passed /tests-passed
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
