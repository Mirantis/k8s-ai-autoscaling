from flask import Flask, Response
import os
import yaml
import requests
import json
import time
import pandas as pd
from prophet import Prophet
from threading import Lock
from datetime import datetime

# Global variables for metrics exposure
latest_forecasts = {}
forecast_lock = Lock()
app = Flask(__name__)

# Configuration loading
CONFIG_DIR = "/config"
with open(os.path.join(CONFIG_DIR, "time_config.yaml")) as f:
    TIME_CONFIG = yaml.safe_load(f)
with open(os.path.join(CONFIG_DIR, "metrics_queries.yaml")) as f:
    METRICS_QUERIES = yaml.safe_load(f)
with open(os.path.join(CONFIG_DIR, "prometheus_config.yaml")) as f:
    PROMETHEUS_CONFIG = yaml.safe_load(f)

TRAINING_HOURS = TIME_CONFIG["training_hours"]
FORECAST_HOURS = TIME_CONFIG["forecast_hours"]
STEP_SIZE = TIME_CONFIG["step_size"]
PROMETHEUS_URL = PROMETHEUS_CONFIG["url"]

def fetch_metrics():
    end_time = int(time.time())
    start_time = end_time - (TRAINING_HOURS * 3600)

    metrics_data = {}

    for metric_name, query in METRICS_QUERIES.items():
        params = {
            "query": query,
            "start": start_time,
            "end": end_time,
            "step": STEP_SIZE
        }

        try:
            response = requests.get(PROMETHEUS_URL, params=params, timeout=40)
            data = response.json().get("data", {}).get("result", [])

            # Extract values for the metric
            if data:
                values = [
                    (int(entry[0]), float(entry[1]))  # (timestamp, value)
                    for entry in data[0].get('values', [])
                ]
                metrics_data[metric_name] = values

        except Exception as e:
            print(f"Error fetching {metric_name}: {str(e)}")

    return metrics_data

def generate_forecasts(metrics_data):
    forecasts = {}

    for metric_name, values in metrics_data.items():
        if len(values) < TIME_CONFIG["min_data_points"]:  # Use config value
            continue

        try:
            # Convert to Prophet format
            df = pd.DataFrame(values, columns=['timestamp', 'y'])
            df['ds'] = pd.to_datetime(df['timestamp'], unit='s')

            # Train model
            model = Prophet(
                daily_seasonality=True,
                weekly_seasonality=True,
            )
            model.add_seasonality(
                name='hourly',
                period=1/24,  # 1 hour in days
                fourier_order=10  # Adjust based on pattern complexity
            )
            model.fit(df)

            periods = int(FORECAST_HOURS * 3600 / STEP_SIZE)
            freq = f"{STEP_SIZE//60}min"
            # Generate forecast
            future = model.make_future_dataframe(
                periods=periods,
                freq=freq
            )
            forecast = model.predict(future)

            # Store predictions
            latest = forecast[['ds', 'yhat']].tail(periods)
            forecasts[metric_name] = {
                'timestamps': latest['ds'].dt.strftime('%Y-%m-%d %H:%M:%S').tolist(),
                'values': latest['yhat'].round(4).tolist()
            }

        except Exception as e:
            print(f"Forecast failed for {metric_name}: {str(e)}")

    return forecasts

def main():
    global latest_forecasts
    metrics_data = fetch_metrics()
    predictions = generate_forecasts(metrics_data)

    with forecast_lock:
        latest_forecasts = predictions  # Store latest forecasts

    print(json.dumps(predictions, indent=2))

# Metrics endpoint implementation
@app.route('/metrics')
def metrics():
    try:
        prom_metrics = []
        with forecast_lock:
            current_time = datetime.now().timestamp()

            # Check if latest_forecasts is empty
            if not latest_forecasts:
                return Response("No forecasts available", status=503, mimetype='text/plain')

            # Generate Prometheus metrics
            for metric_name, forecast in latest_forecasts.items():
                if 'values' in forecast and forecast['values']:
                    first_value = forecast['values'][0]
                    prom_metrics.append(
                        f"forecasted_{metric_name} {first_value} {int(current_time * 1000)}"
                    )

        return Response('\n'.join(prom_metrics), mimetype='text/plain')
    except Exception as e:
        return Response(f"Error generating metrics: {str(e)}", status=500, mimetype='text/plain')

if __name__ == "__main__":
    from threading import Thread

    # Start Flask server in a daemon thread
    Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

    # Main processing loop
    while True:
        try:
            main()
        except Exception as e:
            print(f"Main loop error: {str(e)}")

        # Sleep for 10 seconds between runs
        time.sleep(10)

