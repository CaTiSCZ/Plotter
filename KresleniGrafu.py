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
ax.grid(True, which='both', axis='both', color='lightgrey', linestyle='-', linewidth=0.5)
ax.legend(loc='upper right')

def zoom(event):
    x_pixel, y_pixel = event.x, event.y
    bbox = ax.get_window_extent()

    # Zvětšujeme rozsah bboxu o 15 % dovnitř i ven pro každou osu
    zoom_factor = 0.9 if event.button == 'up' else 1.1

    # Rozšíření okrajů o 15 %
    x_pad = 0.15 * bbox.width
    y_pad = 0.15 * bbox.height

    # Detekce kurzoru v rozšířené oblasti osy X (spodní část)
    if (bbox.y0 - y_pad) <= y_pixel <= (bbox.y0 + y_pad):
        x_min, x_max = ax.get_xlim()
        x_center = event.xdata if event.xdata is not None else (x_min + x_max) / 2
        x_range = (x_max - x_min) * zoom_factor / 2
        ax.set_xlim(x_center - x_range, x_center + x_range)

    # Detekce kurzoru v rozšířené oblasti osy Y (levá část)
    elif (bbox.x0 - x_pad) <= x_pixel <= (bbox.x0 + x_pad):
        y_min, y_max = ax.get_ylim()
        y_center = event.ydata if event.ydata is not None else (y_min + y_max) / 2
        y_range = (y_max - y_min) * zoom_factor / 2
        ax.set_ylim(y_center - y_range, y_center + y_range)

    plt.draw()


# Přiřazení funkce ke skrolování kolečka myši
fig.canvas.mpl_connect('scroll_event', zoom)

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
