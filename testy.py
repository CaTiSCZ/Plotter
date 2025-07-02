import sys
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton

app = QApplication(sys.argv)

window = QWidget()
layout = QVBoxLayout()
window.setLayout(layout)

btn = QPushButton("Send Trigger (CMD 9)")
layout.addWidget(btn)

window.show()
print("Button visible?", btn.isVisible())

sys.exit(app.exec_())
