const path = require("path");

const python =
  process.platform === "win32"
    ? path.join(__dirname, ".venv", "Scripts", "python.exe")
    : path.join(__dirname, ".venv", "bin", "python");

module.exports = {
  apps: [
    {
      name: "server-detective",
      cwd: __dirname,
      script: "main.py",
      interpreter: python,
      args: "--config config.yaml",
      instances: 1,
      autorestart: true,
      watch: false,
      max_restarts: 10,
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
