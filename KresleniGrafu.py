import matplotlib.pyplot as plt
import socket
import time

# UDP nastavení
UDP_IP = "0.0.0.0"  # přijímat na všech rozhraních
UDP_PORT = 9999
BUFFER_SIZE = 1024

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT))
sock.settimeout(0.1)  # krátký timeout pro plynulý loop

# Nastavení grafu
plt.ion()
fig, ax = plt.subplots()
line, = ax.plot([], [], 'b-', label='sin(x)')
ax.set_ylim(-1.1, 1.1)
ax.set_xlim(0, 10)  # Rozsah osy X bude stále 10 jednotek (0 až 10)
ax.set_title('Zobrazení sinusoidy z UDP')
ax.set_xlabel('x')
ax.set_ylabel('sin(x)')
ax.grid(True, which='both', axis='both', color='lightgrey', linestyle='-', linewidth=0.5)
ax.legend(loc='upper right')

# Seznam pro uložení dat
x_data = []
y_data = []

# Čas pro zkrácení vykreslování (max 30x za sekundu)
last_redraw = time.time()
redraw_interval = 1 / 30  # max 30x za sekundu

# Flag pro výpis stavu
waiting_for_data = True

# Funkce pro resetování dat a grafu
def reset_graph():
    global x_data, y_data, last_redraw
    print("Resetování grafu...")
    x_data = []
    y_data = []
    line.set_data(x_data, y_data)
    ax.set_xlim(0, 10)  # Resetování osy X
    ax.set_ylim(-1.1, 1.1)  # Resetování osy Y
    plt.draw()
    last_redraw = time.time()

print("Plotter spuštěn. Čekám na UDP data...")

try:
    while plt.fignum_exists(fig.number):
        try:
            # Čekání na UDP data
            data, addr = sock.recvfrom(BUFFER_SIZE)
            text = data.decode('utf-8').strip()

            # Zpracování příchozích dat
            new_data = False
            for line_text in text.splitlines():
                try:
                    x_str, y_str = line_text.strip().split()
                    x = float(x_str)
                    y = float(y_str)

                    # Přidání dat do seznamu
                    x_data.append(x)
                    y_data.append(y)
                    new_data = True  # Pokud přijdou nová data, označíme to

                except ValueError:
                    continue  # Pokud je formát dat neplatný, ignorujeme

            if new_data:
                if waiting_for_data:  # Vypíšeme pouze jednou, když se začnou přijímat data
                    print("Data přijata...")
                    waiting_for_data = False  # Po první informaci o přijetí dat nebudeme už nic vypisovat
                    reset_graph()  # Reset grafu při přechodu na přijímání dat
            else:
                if not waiting_for_data:
                    print("Čekám na data...")
                    waiting_for_data = True  # Změníme stav na čekání, aby další výpis byl o čekání na data

        except socket.timeout:
            # Pokud čekáme na data, ale žádná nepřichází
            if not waiting_for_data:
                print("Čekám na data...")
                waiting_for_data = True  # Změníme stav na čekání, aby další výpis byl o čekání na data

        # Posunování dat na ose X tak, aby nová data byla na pravé straně grafu
        if len(x_data) > 0:
            # Posuneme data vlevo, aby nová data byla na pravé straně
            while x_data[-1] - x_data[0] > 10:
                x_data.pop(0)  # Odebereme první bod
                y_data.pop(0)  # Odebereme odpovídající hodnotu z Y

        # Kontrola, zda uplynul čas pro vykreslení (max 30x za sekundu)
        if x_data and y_data and time.time() - last_redraw >= redraw_interval:
            # Zobrazení dat a posun rozsahu osy X
            line.set_data(x_data, y_data)
            ax.set_xlim(x_data[0], x_data[0] + 10)  # Osa X má rozsah 10 jednotek
            plt.draw()
            plt.pause(0.001)  # malý čas pro aktualizaci
            last_redraw = time.time()

except KeyboardInterrupt:
    print("\nUkončeno uživatelem klávesnicí.")

finally:
    sock.close()
    plt.ioff()
    plt.close(fig)
    print("Program ukončen.")
