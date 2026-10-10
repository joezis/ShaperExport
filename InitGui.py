import FreeCADGui as Gui
import FreeCAD as App
import os
import sys

try:
    _plugin_dir = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _candidates = [
        os.path.join(App.getHomePath(), "Mod", "ShaperExport"),
        os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "Mod", "ShaperExport"),
        os.path.join(os.path.dirname(sys.executable), "Mod", "ShaperExport"),
    ]
    _plugin_dir = next((path for path in _candidates if os.path.isdir(path)), _candidates[0])
if _plugin_dir in sys.path:
    sys.path.remove(_plugin_dir)
sys.path.insert(0, _plugin_dir)

class ExportCommand:
    @staticmethod
    def _tr(de_text, en_text):
        languages = []
        try:
            languages.append(App.ParamGet("User parameter:BaseApp/Preferences/General").GetString("Language", ""))
        except Exception:
            pass
        try:
            languages.append(App.getLocale())
        except Exception:
            pass
        try:
            try:
                from PySide import QtCore
            except ImportError:
                try:
                    from PySide2 import QtCore
                except ImportError:
                    from PySide6 import QtCore
            languages.append(QtCore.QLocale().name())
        except Exception:
            pass
        is_german = any(str(language).lower().startswith(("de", "german", "deutsch"))
                        for language in languages if language)
        return de_text if is_german else en_text

    def GetResources(self):
        return {"MenuText": self._tr("Für Shaper exportieren ...", "Export SVG..."),
                "ToolTip": self._tr("Ausgewählte Geometrie als Shaper exportieren", "Export selected geometry as SVG")}
    def IsActive(self):
        return Gui.activeDocument() is not None
    def Activated(self):
        selected = Gui.Selection.getSelection()
        shape2d_views = [obj for obj in selected
                         if "shape2dview" in str(getattr(obj, "TypeId", "")).lower()
                         or "shape2dview" in str(getattr(obj, "Name", "")).lower()
                         or "shape2dview" in str(getattr(obj, "Label", "")).lower()]
        if not shape2d_views:
            message = self._tr("Bitte wähle zuerst ein Shape2DView-Objekt aus.",
                               "Please select a Shape2DView object first.")
            try:
                from PySide import QtWidgets
                message_box = QtWidgets.QMessageBox
            except (ImportError, AttributeError):
                try:
                    from PySide2 import QtWidgets
                    message_box = QtWidgets.QMessageBox
                except (ImportError, AttributeError):
                    try:
                        from PySide6 import QtWidgets
                        message_box = QtWidgets.QMessageBox
                    except (ImportError, AttributeError):
                        from PySide import QtGui
                        message_box = QtGui.QMessageBox
            message_box.information(Gui.getMainWindow(), self._tr("Shaper-Export", "Shaper Export"), message)
            return
        # FreeCAD executes InitGui.py as a standalone script, so it has no
        # package context for a relative import.
        sys.modules.pop("shaper_export", None)
        import shaper_export
        shaper_export.export_selection()

Gui.addCommand("ShaperExport_Export", ExportCommand())

class ShaperExportMenuManipulator:
    """Contribute the command to the File menu only in the Draft workbench."""
    def modifyMenuBar(self):
        try:
            import FreeCADGui as FreeCADGui
            active = FreeCADGui.activeWorkbench()
            active_name = next((name for name, handler in FreeCADGui.listWorkbenches().items()
                                if handler is active), "")
        except Exception:
            active_name = ""

        # Remove obsolete menu groups and the command from other workbenches.
        changes = [
            {"remove": "ShaperExport_Export"},
            {"remove": "Shaper Export"},
            {"remove": "Shaper-Export"},
        ]
        if active_name == "DraftWorkbench":
            changes.append({"insert": "ShaperExport_Export",
                            "menuItem": "Std_PrintPdf",
                            "after": True})
        return changes

try:
    Gui.addWorkbenchManipulator(ShaperExportMenuManipulator())
except Exception as exc:
    App.Console.PrintError("ShaperExport: Workbench menu registration failed: %s\n" % exc)
