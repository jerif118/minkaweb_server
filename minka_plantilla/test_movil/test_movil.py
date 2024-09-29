import socketio

sio = socketio.Client()

server_link = 'http://127.0.0.1:5000'
predefined_token = "gAAAAABmviJk-PdAGrx27W_zGi5ghB9oCSWMQnBHWOWbZuHU_d3ullXpOXqLKYJMKy_QmdnML-J1PrDKAFqd54U2DnAfdztIBg_gBOnfx-r2Cxj6IrX3TCBdeNGVZ2X9oe63wGzl-xUjs8RM6jCGePEpyUp6MEykeX7JCJYIGDE8S41-5EXF9vG1jjd7udiE36FG9IYTnLxoAOlWBUeVNvgXUJ0eXTtIYlguwNUm1T4pyHp-BoXletDmbrmDPLUcnyCbs2GxGCGFk3JCaFT0XxTCVWQ2HP_ytg=="

def connect_mobile():
    try:
        # Conectar al servidor WebSocket con el token predefinido
        sio.connect(server_link, headers={'Authorization': predefined_token})
        print("Mobile client connected to the WebSocket server")
        # Mantener la conexión activa
        sio.wait()
    except Exception as e:
        print(f"Failed to connect: {e}")

@sio.event
def connect():
    print("Mobile client WebSocket connection established")

@sio.event
def disconnect():
    print("Mobile client disconnected from the WebSocket server")

@sio.event
def connect_error(data):
    print("The connection failed!")

if __name__ == '__main__':
    connect_mobile()
