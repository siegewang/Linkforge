# Use a lightweight, official Python runtime
FROM python:3.12-slim

# Set the working directory inside the container
WORKDIR /app

# Install git for live system updates
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN git config --global --add safe.directory /app && git config --global --add safe.directory *

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the port Flask runs on
EXPOSE 5000

# Command to run the application
CMD ["python", "app.py"]
