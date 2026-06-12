import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('75.119.154.255', username='root', password='8i8Jlnuyz~2cKisB', timeout=30)
stdin, stdout, stderr = client.exec_command("docker ps --format '{{.Names}}'")
out = stdout.read().decode()
err = stderr.read().decode()
print("Containers:")
print(out)
if err:
    print("ERR:", err)
client.close()
