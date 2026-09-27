from flask import Flask

app = Flask(__name__)


@app.get("/")
def index():
    return "demo-service-1 is up\n"


@app.get("/health")
def health():
    return {"status": "ok", "service": "demo-service-1"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
