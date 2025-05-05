import matplotlib.pyplot as plt
import socket
import time

# UDP nastavení
UDP_IP = "0.0.0.0"  # přijímat na všech rozhraních
UDP_PORT = 9999
BUFFER_SIZE = 1024

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT))
sock.settimeout(0.01)  # krátký timeout pro plynulý loop

# Nastavení grafu
plt.ion()
fig, ax = plt.subplots()
line, = ax.plot([], [], 'b-', label='sin(x)')
ax.set_ylim(-1.1, 1.1)
ax.set_xlim(0, 10)
ax.set_title('Zobrazení sinusoidy z UDP')
ax.set_xlabel('x')
ax.set_ylabel('sin(x)')
ax.grid(True, which='both', axis='both', color='lightgrey', linestyle='-', linewidth=0.5)
ax.legend(loc='upper right')

x_data = []
y_data = []

print("Plotter spuštěn. Čekám na UDP data...")

try:
    while plt.fignum_exists(fig.number):
        try:
            data, addr = sock.recvfrom(BUFFER_SIZE)
            text = data.decode('utf-8').strip()
            for line_text in text.splitlines():
                x_str, y_str = line_text.strip().split()
                x = float(x_str)
                y = float(y_str)
                x_data.append(x)
                y_data.append(y)

                # Posouvání okna na ose X
                if x > ax.get_xlim()[1]:
                    shift = x - ax.get_xlim()[1]
                    ax.set_xlim(ax.get_xlim()[0] + shift, ax.get_xlim()[1] + shift)

        except socket.timeout:
            pass  # žádná data, pokračuj

        line.set_data(x_data, y_data)
        plt.draw()
        plt.pause(0.01)

except KeyboardInterrupt:
    print("\nUkončeno uživatelem klávesnicí.")

finally:
    sock.close()
    plt.ioff()
    plt.close(fig)
    print("Program ukončen.")
