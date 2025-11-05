from flask import Flask
from routes.macros_routes import macros_bp
from routes.mealplan_routes import mealplan_bp

app = Flask(__name__)

# Register blueprints
app.register_blueprint(macros_bp)
app.register_blueprint(mealplan_bp)


@app.route("/")
def home():
    return "Hello from Flask API on Heroku!"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)