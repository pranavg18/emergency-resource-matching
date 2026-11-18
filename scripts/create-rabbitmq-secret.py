"""Create demo broker credentials once; never print or overwrite a password."""
import json
import secrets
import subprocess


def main():
    subprocess.run(["kubectl", "apply", "-f", "deploy/k8s/namespace.yaml"], check=True)
    result = subprocess.run(
        ["kubectl", "-n", "emergency", "get", "secret", "rabbitmq-credentials",
         "--ignore-not-found", "-o", "name"], capture_output=True, text=True, check=True)
    if result.stdout.strip():
        print("rabbitmq-credentials already exists; leaving it unchanged.")
        return
    secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
              "metadata": {"name": "rabbitmq-credentials", "namespace": "emergency"},
              "stringData": {"username": "emergency", "password": secrets.token_hex(24)}}
    # stdin keeps the password out of process arguments, files and shell history.
    subprocess.run(["kubectl", "create", "-f", "-"], input=json.dumps(secret),
                   text=True, check=True)


if __name__ == "__main__":
    main()
