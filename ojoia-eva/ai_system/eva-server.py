import uvicorn
from api_eva import app
if __name__ == "__main__":
    uvicorn.run(app, host='0.0.0.0', port=8007, log_level='warning')
