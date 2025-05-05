# sin_plotter.py
import matplotlib.pyplot as plt
import time
from sinus_generator import SinusGenerator

# Inicializace generátoru
gen = SinusGenerator(dx=0.1, delay=0.01)
gen.start()

# Nastavení grafu
plt.ion()
fig, ax = plt.subplots()
line, = ax.plot([], [], 'b-', label='sin(x)')
ax.set_ylim(-1.1, 1.1)
ax.set_xlim(0, 10)
ax.set_title('Zobrazení sinusoidy z generátoru')
ax.set_xlabel('x')
ax.set_ylabel('sin(x)')
ax.grid(True)
ax.legend(loc='upper right')

print("Plotter spuštěn. Ukonči pomocí Ctrl+C nebo zavři okno grafu.")

try:
    while plt.fignum_exists(fig.number):  # Okno grafu stále existuje
        data = gen.get_data()
        if data:
            x_data, y_data = zip(*data)

            # Posouvání okna na ose X
            if x_data[-1] > ax.get_xlim()[1]:
                shift = x_data[-1] - ax.get_xlim()[1]
                ax.set_xlim(ax.get_xlim()[0] + shift, ax.get_xlim()[1] + shift)

            line.set_data(x_data, y_data)
            plt.draw()
            plt.pause(0.01)
        else:
            time.sleep(0.01)

except KeyboardInterrupt:
    print("\nUkončeno uživatelem klávesnicí.")

finally:
    gen.stop()
    plt.ioff()
    plt.close(fig)
    print("Program ukončen.")
