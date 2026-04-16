# Usa una imagen oficial de Python como imagen base
FROM python:3.9-slim

# Establece el directorio de trabajo en /app
WORKDIR /app

# Copia el archivo de requisitos (que está en el mismo directorio que el Dockerfile)
# al directorio de trabajo /app
COPY requirements.txt .

# Instala las dependencias
RUN pip install --no-cache-dir -r requirements.txt

# Copia el contenido del directorio 'minka_plantilla/server'
# (relativo al Dockerfile) al directorio '/app/server' dentro de la imagen.
COPY minka_plantilla/server ./server

# Expone el puerto en el que corre la aplicación
EXPOSE 5001

# Comando para correr la aplicación cuando el contenedor inicie
CMD ["python", "server/run.py"]