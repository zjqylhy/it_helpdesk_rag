import os
import uvicorn

if __name__ == '__main__':
    uvicorn.run('helpdesk.api:app', host=os.getenv('RAG_HOST','127.0.0.1'),
                port=int(os.getenv('RAG_PORT','8000')), loop='helpdesk.runtime:loop_factory')
