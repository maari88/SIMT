import os
from flask import Flask
import redis

app = Flask(__name__)
cache = redis.Redis(host='db', port=6379)

@app.route('/')
def hello():
    count = cache.incr('hits')
    return f'Hello {count} times!\n'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)