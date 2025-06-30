Plotter:
Komunikace přes UDP
buffer 1 (síť) - řažení vzorků a počítání chybějících packetů XX
Buffer grafu - vždy plný, vykresluje se z něj graf
ukládání dat do souboru - na trigger ([čas] před až konec bufferu) nebo uložit celý buffer
vyřešit číslování souborů při ukládání, vždy když mám stejné jméno tak uložím o jedna větší index než mám ve složce

GUI
	GRAF
		dvě osy XX
		Nastavování os podle ID packetu XX
	zaplnění bufferu (buffer sítě - buffer jedna) XX 
	ERR chybné packety XX
	ERR chybějící packety XX
	ERR chybné vzorky
	počítadlo celkově přijatých packetů XX
	reset errorových počítadel
	Možnost zdání portu pro příjem dat a odesílání CMD
	zatržítko jestli poslouchat data na všech IP nebo jen na IP odkud chodí CMD XX 
	možnost zadání ip a portu kam se posílají CMD
	log message 
	
	
	Tlačítka: PING, ID Packet, Register receiver (pole pro zadání adresy), Remove receiver (pole pro zadání adresy), Get receiver
	Automatizační tlačítko conect provede: get ID, register receiver s adresou plotteru a vypíše connect při úspěškosti obou. XX
	Tlačítka pro data: Start, start on trigger a stop (dořešit trigger)
	možnost zadat požadovaný počet packetů (0=kontinualnmí)
	set path a save buffer, pole safe before trigger [čas] XX
	Ad Hoc Save tlačítko pro jednorázové uložení bufferu jinam
	výpis path - i s jménem následujícího souboru
	pole pro nastavení velikosti bufferu v s
	trigger tlačítko, zatržítko safe on trigger



pamatovat si poslední nastavení - ukládání do souboru a čtení posledního nastavení ze souboru při startu - soubor musí být čitelný pro lidi - ini standart 
vyřešit mechanismy čtení dat z více zařízení
	

		

		