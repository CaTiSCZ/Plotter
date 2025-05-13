"""
spustím program 
Načítání dat přes UDP 
zobrazím GUI
    graf se dvěma Y
    zaplnění bufferu
    zadání adresy zdroje dat s žádostí o data
    errory
    info
po zadání adresy a požádání si řeknu o identifikační packet, uložím si z něj data do proměných
    Identifikační packet:
        zjistím počet kanálů a pořadí dle typu dat
        zadám přepočet pro každý kanál
        nastavím osy
        zobrazím informace o hardware a firmware
        informace o úspěšné identifikaci (conectit/disconectit)

zažádám o data tlačítkem (změna tlačítka na ukončovací sběru dat)

datový packet: 
seřazení packetů v bufferu podle pořadového čísla a vykreslení vždy za 1/30 s, kontrola chybějících packetů
provést CRC a případně označit packet jako chybný
zjistit počet špatných vzorů z hlavičky 


jeden packet s více typy dat (200 od jednoho typu, pak 200 od dalšího)
ukládání dat do bufferu
vykreslování dat z bufferu do grafu podle přepočtu a typu dat
dvě osy Y (U a I)
mazání dat z bufferu po vykreslení
vykreslování naplněnosti bufferu - procenta a počet uložrených vzorků
zobrazit zpoždění - ulažená data ku rychlosti příchodu dat

errory: 
    počet stracených packetů
    počet špatně přečtených dat
    počet špatně přenesených packetů (CRC) vůči správným (jak absolutně tak v procentu)

"""