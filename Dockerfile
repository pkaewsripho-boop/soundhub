# สำหรับรันบนเซิร์ฟเวอร์ออนไลน์ (Hugging Face Spaces, Render ฯลฯ)
FROM python:3.11-slim
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1 AIOHTTP_NOSENDFILE=1
WORKDIR /home/user/app
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt
COPY --chown=user . .
EXPOSE 7860
CMD ["sh", "-c", "python server.py --no-browser --port ${PORT:-7860}"]
