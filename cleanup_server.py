import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('75.119.154.255', username='root', password='8i8Jlnuyz~2cKisB', timeout=30)

print("Removing cron job...")
stdin, stdout, stderr = client.exec_command('(crontab -l 2>/dev/null | grep -v "tsa_deployments") | crontab -')
print(stdout.read().decode().strip())
err = stderr.read().decode().strip()
if err:
    print(f"stderr: {err}")

print("\nRemoving remote directory...")
stdin, stdout, stderr = client.exec_command('rm -rf /opt/tsa_deployments')
print(stdout.read().decode().strip())
err = stderr.read().decode().strip()
if err:
    print(f"stderr: {err}")

print("\nVerifying cron cleanup...")
stdin, stdout, stderr = client.exec_command('crontab -l 2>/dev/null || echo "No crontab"')
crontab_output = stdout.read().decode().strip()
print(f"Current crontab: {crontab_output if crontab_output else 'Empty'}")

client.close()
print("\nServer cleanup done.")
