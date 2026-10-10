# ShaperExport

ShaperExport ist ein Zusatz für FreeCAD. Er exportiert ausgewählte Shape-Objekte als SVG-Datei.
Die ausgegebene SVG ist für die Fräsmaschine **Shaper Origin** optimiert. Shaper Origin
interpretiert die Farben von Linien und Flächenfüllungen als Codierung für die Art der
Fräsungen; die im Export gesetzten Farben sind daher Teil der Fertigungsanweisung.

## Installation

1. Den Ordner `ShaperExport` in das FreeCAD-Verzeichnis `Mod` kopieren.
2. FreeCAD vollständig neu starten.
3. In der Draft-Arbeitsumgebung die gewünschten `Shape2DView`-Objekte auswählen.
4. Im Menü **Datei**, direkt unter **PDF exportieren…**, **Für Shaper exportieren ...** wählen.
5. Speicherort und Dateinamen im Dialog festlegen.

Der Menüeintrag ist für die Draft-Arbeitsumgebung vorgesehen. FreeCAD muss mindestens Version 1.0 verwenden.

## Exportverhalten

- Die Geometrie wird auf die XY-Ebene projiziert; Z-Koordinaten werden verworfen.
- Die Objektplatzierung wird, soweit möglich, auf die exportierte Geometrie angewendet.
- Linien und Kreisbögen werden anhand ihrer Endpunkte verbunden. Für das Verbinden von SVG-Endpunkten gilt eine Toleranz von `0.001` SVG-Einheiten.
- Geschlossene Umrisse werden mit dem SVG-Schließbefehl `z` ausgegeben; es werden keine künstlichen Schlusslinien ergänzt. Nicht schließbare offene Linienzüge bleiben offen und werden als graue On-line-Schnitte ausgegeben.
- Kreise und Kreisbögen bleiben als SVG-Elemente beziehungsweise SVG-Bögen erhalten. Konturen bleiben in getrennten SVG-Elementen.
- Vor dem Speichern erscheint der Dialog **Taschengröße**. Er fragt eine maximale Breite und Höhe in Millimetern ab; derselbe Grenzwert gilt für beide Maße und wird für den nächsten Export gespeichert. Abbrechen beendet den Export. Konturen, deren Breite und Höhe innerhalb dieses Grenzwerts liegen, erhalten die graue Taschenfüllung `#777777` und `stroke="none"`. Weitere innenliegende Konturen erhalten weiße Füllung und schwarze Kontur; Außenkonturen erhalten schwarze Füllung und schwarze Kontur. Verschachtelung wird per Punkt-in-Polygon-Prüfung erkannt; Kreisbögen werden dafür angenähert abgetastet.
- Alle Konturen erhalten `stroke-width="0.01"` in SVG-Benutzereinheiten. Da `width`/`height` in mm und die passende `viewBox` denselben Zahlenbereich verwenden, entspricht das 0,01 mm. Die Taschen behalten `stroke="none"`, damit ihre graue Füllung die Taschen-Farbcodierung allein bestimmt.
- Redundante Punkte werden mit einer maximalen Abweichung von `0.01 mm` vereinfacht.
- Nach dem Export werden die Anzahl der Konturen und eine Überlappungsstatistik angezeigt.

## Überlappungsstatistik

Die Statistik zählt Objektpaare mit überlappenden oder sich berührenden XY-Begrenzungsboxen. Sie ist eine Näherung und prüft nicht, ob sich die tatsächlichen Konturen oder Flächen schneiden.

## Hinweise und Grenzen

- Es werden ausgewählte Objekte mit Shape-Kanten exportiert. Objekte ohne Shape-Kanten liefern keine SVG-Geometrie.
- Die Geometrie wird auf XY projiziert; räumliche Höhen und Z-Abstände bleiben im SVG nicht erhalten.
- FreeCADs Menü- und GUI-Integration hängt von der installierten FreeCAD-Version ab. Nach Änderungen am Plugin muss FreeCAD neu gestartet werden.

## Lizenz

Dieses Plugin steht unter der GNU General Public License Version 3 (**GPL-3.0-only**). Siehe [`LICENSE`](LICENSE) für den vollständigen Lizenztext.
