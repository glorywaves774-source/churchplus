web: gunicorn run:app --workers ${WEB_CONCURRENCY:-2} --threads 4 --timeout 60 --bind 0.0.0.0:${PORT:-8000} --access-logfile - --error-logfile -
release: flask db upgrade
