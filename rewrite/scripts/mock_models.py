"""仅供本机联测的无凭据 HTTP 替身，不能配置为正式模型。"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


class Handler(BaseHTTPRequestHandler):
    generation_counts = {}

    def log_message(self, *_):
        pass

    def do_GET(self):
        encoded = json.dumps({'generation_counts': self.generation_counts}).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded))); self.end_headers(); self.wfile.write(encoded)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/embed':
            result = {'data': [{'index': i, 'embedding': [1., .2, .3]} for i, _ in enumerate(body['input'])],
                      'usage': {'prompt_tokens': 5, 'completion_tokens': 0}}
        elif self.path == '/rank':
            result = {'results': [{'index': i, 'relevance_score': 1-i/1000} for i, _ in enumerate(body['documents'])],
                      'usage': {'input_tokens': 5, 'output_tokens': 0}}
        elif body.get('stream'):
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            prompt = json.loads(body['messages'][1]['content'])
            key = prompt['question']
            self.generation_counts[key] = self.generation_counts.get(key, 0) + 1
            try:
                # 慢流给浏览器留出取消窗口；其他请求仍由服务线程处理。
                if '中断' in prompt['question']: time.sleep(2)
                for text in ['请打开', '设置 [C1]。']:
                    self.wfile.write(('data: ' + json.dumps({'choices': [{'delta': {'content': text}, 'finish_reason': None}]}, ensure_ascii=False) + '\n\n').encode()); self.wfile.flush()
                    if text == '请打开' and '后台执行' in key: time.sleep(6)
                    time.sleep(.05)
                self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":5}}\n\ndata: [DONE]\n\n'); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return
        else:
            payload = json.loads(body['messages'][1]['content'])
            if 'available_knowledge_bases' in payload:
                value = {'query': payload['question'], 'behavior': 'search', 'kb_ids': [k['id'] for k in payload['available_knowledge_bases']], 'requires_all': False, 'message': '', 'option_ids': []}
            elif 'history' in payload:
                value = {'query': payload['question']}
            else:
                value = {'behavior': 'answer', 'reason': 'synthetic fixture', 'evidence_labels': ['C1']}
            result = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 2}}
        encoded = json.dumps(result).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(encoded))); self.end_headers(); self.wfile.write(encoded)


def start():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    return server
