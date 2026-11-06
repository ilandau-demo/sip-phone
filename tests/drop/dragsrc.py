# A tiny GTK window that is a drag source for one file (text/uri-list).
import sys, gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib
path = sys.argv[1]
uri = GLib.filename_to_uri(path, None)
w = Gtk.Window(title="dragsrc"); w.move(0, 0); w.set_default_size(300, 300); w.set_decorated(False)
b = Gtk.Button(label="DRAG ME"); w.add(b)
b.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [], Gdk.DragAction.COPY)
b.drag_source_add_uri_targets()
b.connect("drag-data-get", lambda wid, ctx, sel, info, t: sel.set_uris([uri]))
b.connect("drag-end", lambda *a: print("drag-end", flush=True))
w.connect("destroy", Gtk.main_quit)
w.show_all()
print("ready", flush=True)
Gtk.main()
