import os
import math
import re
import tempfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr
import FreeCAD as App
import FreeCADGui as Gui

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

def _xy(p): return (float(p.x), float(p.y))
def _collect(selection):
    edges=[]; boxes=[]; native=[]
    for obj in selection:
        shape=getattr(obj,"Shape",None)
        if shape is None: continue
        try: shape=shape.copy(); shape.Placement=obj.getGlobalPlacement()
        except Exception: pass
        for edge in shape.Edges:
            curve=edge.Curve
            try:
                if hasattr(curve,"Radius") and hasattr(curve,"Center"):
                    cx,cy=_xy(curve.Center); radius=float(curve.Radius)
                    first,last=float(edge.FirstParameter),float(edge.LastParameter)
                    delta=last-first
                    if abs(abs(delta)-2.0*math.pi)<1e-7:
                        native.append(("circle",cx,cy,radius))
                    else:
                        start=_xy(edge.valueAt(first)); end=_xy(edge.valueAt(last))
                        if str(edge.Orientation)=="Reversed":
                            start,end=end,start; delta=-delta
                        native.append(("arc",cx,cy,radius,start,end,delta))
                    continue
            except Exception:
                pass
            try:
                n=max(24,int(abs(edge.LastParameter-edge.FirstParameter)*getattr(edge.Curve,"Radius",1)/1.5))
                pts=[_xy(edge.valueAt(edge.FirstParameter+(edge.LastParameter-edge.FirstParameter)*i/n)) for i in range(n+1)]
            except Exception:
                try: pts=[_xy(v.Point) for v in edge.Vertexes]
                except Exception: pts=[]
            if len(pts)>1: edges.append(pts)
        try:
            b=shape.BoundBox; boxes.append((b.XMin,b.YMin,b.XMax,b.YMax))
        except Exception: pass
    return edges,boxes,native

def _chains(edges):
    unused=set(range(len(edges))); starts={}; ends={}
    for i,e in enumerate(edges): starts.setdefault(e[0],[]).append(i); ends.setdefault(e[-1],[]).append(i)
    paths=[]
    while unused:
        i=min(unused); unused.remove(i); chain=list(edges[i])
        while True:
            candidates=[j for j in starts.get(chain[-1],()) if j in unused]; reverse=False
            if not candidates: candidates=[j for j in ends.get(chain[-1],()) if j in unused]; reverse=True
            if not candidates: break
            j=min(candidates); unused.remove(j); following=list(reversed(edges[j])) if reverse else edges[j]; chain.extend(following[1:])
        paths.append((chain,len(chain)>2 and chain[0]==chain[-1]))
    return paths

def _overlaps(boxes):
    return sum(1 for i,a in enumerate(boxes) for b in boxes[i+1:] if min(a[2],b[2])>=max(a[0],b[0]) and min(a[3],b[3])>=max(a[1],b[1]))

def _distance_to_segment_sq(point, start, end):
    dx=end[0]-start[0]; dy=end[1]-start[1]
    length_sq=dx*dx+dy*dy
    if length_sq==0:
        return (point[0]-start[0])**2+(point[1]-start[1])**2
    t=max(0.0,min(1.0,((point[0]-start[0])*dx+(point[1]-start[1])*dy)/length_sq))
    px=start[0]+t*dx; py=start[1]+t*dy
    return (point[0]-px)**2+(point[1]-py)**2

def _simplify_open(points, tolerance):
    """Ramer-Douglas-Peucker simplification; keep both original endpoints."""
    if len(points)<=2: return points[:]
    keep={0,len(points)-1}; stack=[(0,len(points)-1)]; limit_sq=tolerance*tolerance
    while stack:
        first,last=stack.pop(); greatest=limit_sq; index=None
        for i in range(first+1,last):
            distance=_distance_to_segment_sq(points[i],points[first],points[last])
            if distance>greatest: greatest=distance; index=i
        if index is not None:
            keep.add(index); stack.append((first,index)); stack.append((index,last))
    return [points[i] for i in sorted(keep)]

def _simplify(points, tolerance=0.01):
    """Reduce redundant vertices within 0.01 mm while preserving endpoints."""
    if len(points)<=3: return points[:]
    if points[0]!=points[-1]:
        return _simplify_open(points,tolerance)
    ring=points[:-1]
    if len(ring)<=3: return points[:]
    start=ring[0]
    opposite=max(range(1,len(ring)),key=lambda i:(ring[i][0]-start[0])**2+(ring[i][1]-start[1])**2)
    first_half=_simplify_open(ring[:opposite+1],tolerance)
    second_half=_simplify_open(ring[opposite:]+[start],tolerance)
    return first_half[:-1]+second_half[:-1]+[start]

def _svg(edges,native):
    xs=[p[0] for e in edges for p in e]; ys=[p[1] for e in edges for p in e]
    for item in native:
        if item[0]=="circle":
            _,cx,cy,r=item; xs.extend((cx-r,cx+r)); ys.extend((cy-r,cy+r))
        else:
            _,cx,cy,r,start,end,delta=item
            xs.extend((start[0],end[0])); ys.extend((start[1],end[1]))
            a0=math.atan2(start[1]-cy,start[0]-cx)
            for angle in (0.0,math.pi/2,math.pi,3*math.pi/2):
                progress=((angle-a0)%(2*math.pi) if delta>=0 else (a0-angle)%(2*math.pi))
                if progress<=abs(delta)+1e-10:
                    xs.append(cx+r*math.cos(angle)); ys.append(cy+r*math.sin(angle))
    x0,y0=min(xs),min(ys); w=max(max(xs)-x0,1e-6); h=max(max(ys)-y0,1e-6)
    root=ET.Element("svg",{"xmlns":"http://www.w3.org/2000/svg","viewBox":"0 0 %s %s"%(w,h),"width":"%smm"%w,"height":"%smm"%h})
    group=ET.SubElement(root,"g",{"transform":"translate(0 %s) scale(1 -1)"%h,"fill":"none","stroke":"black"})
    for item in native:
        if item[0]=="circle":
            _,cx,cy,r=item
            ET.SubElement(group,"circle",{"cx":str(cx-x0),"cy":str(cy-y0),"r":str(r)})
        else:
            _,cx,cy,r,start,end,delta=item
            large="1" if abs(delta)>math.pi else "0"
            # The group flips Y, so positive FreeCAD angles use SVG sweep 1.
            sweep="1" if delta>=0 else "0"
            d="M %s %s A %s %s 0 %s %s %s %s"%(start[0]-x0,start[1]-y0,r,r,large,sweep,end[0]-x0,end[1]-y0)
            ET.SubElement(group,"path",{"d":d})
    for pts,closed in _chains(edges):
        pts=_simplify(pts)
        pts=[(x-x0,y-y0) for x,y in pts]; d="M %s %s "%pts[0]+" ".join("L %s %s"%p for p in pts[1:])
        if closed: d+=" Z"
        ET.SubElement(group,"path",{"d":d})
    return ET.tostring(root,encoding="utf-8",xml_declaration=True)

def _line_path_points(data):
    """Parse FreeCAD's simple absolute M/L/Z paths; leave curves untouched."""
    tokens=re.findall(r"[A-Za-z]|[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?",data)
    if not tokens or any(t.isalpha() and t not in ("M","L","Z") for t in tokens):
        return None
    points=[]; closed=False; current=(0.0,0.0); command=None; i=0
    try:
        while i<len(tokens):
            if tokens[i].isalpha():
                command=tokens[i]; i+=1
                if command=="Z":
                    closed=True
                    if points and points[-1]==points[0]: points.pop()
                    current=points[0]
                    command=None
                    continue
                if command=="M" and points:
                    return None  # multiple subpaths must remain unchanged
            if command not in ("M","L") or i+1>=len(tokens): return None
            x,y=float(tokens[i]),float(tokens[i+1]); i+=2
            point=(x,y)
            if command=="M" and points: command="L"
            points.append(point); current=point
        if len(points)<2:return None
    except (ValueError,IndexError):
        return None
    return points,closed


def _line_style_key(element):
    style_names=("fill","stroke","stroke-width","style","fill-rule","opacity",
                 "fill-opacity","stroke-opacity","stroke-linecap","stroke-linejoin")
    return tuple((name,element.attrib[name]) for name in style_names if name in element.attrib)

def _is_stroke_only(element):
    fill=element.attrib.get("fill")
    if fill is None:
        for declaration in element.attrib.get("style","").split(";"):
            if declaration.strip().startswith("fill:"):
                fill=declaration.split(":",1)[1].strip()
                break
    return fill in (None,"none")

def _close_svg_subpaths(data):
    """Close every SVG subpath and make the final close command lowercase z."""
    # Exclude e/E from the command matcher because they occur in exponents.
    segments=re.findall(r"[A-DF-Za-df-z][^A-DF-Za-df-z]*",data)
    if not segments:return data
    output=[]; current=None; start=None; explicitly_closed=False
    number_re=r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?"
    for segment in segments:
        command=segment[0]; values=[float(v) for v in re.findall(number_re,segment[1:])]
        upper=command.upper(); relative=command.islower()
        if upper=="M":
            if start is not None and not explicitly_closed:
                output.append("z")
            if len(values)<2 or len(values)%2:return data
            x,y=values[0],values[1]
            if relative and current is not None:x+=current[0];y+=current[1]
            current=(x,y);start=current;explicitly_closed=False;output.append(segment)
            # Additional moveto coordinate pairs are implicit lineto pairs.
            for i in range(2,len(values),2):
                x,y=values[i],values[i+1]
                if relative:x+=current[0];y+=current[1]
                current=(x,y)
            continue
        if upper=="Z":
            output.append("z");current=start;explicitly_closed=True;continue
        if current is None:return data
        sizes={"L":2,"T":2,"H":1,"V":1,"C":6,"S":4,"Q":4,"A":7}
        size=sizes.get(upper)
        if size is None or len(values)<size or len(values)%size:return data
        for i in range(0,len(values),size):
            args=values[i:i+size]
            if upper in ("H","V"):
                x=current[0] if upper=="V" else args[0]
                y=current[1] if upper=="H" else args[0]
                if relative:
                    if upper=="H":x=current[0]+args[0]
                    else:y=current[1]+args[0]
            else:
                x,y=args[-2],args[-1]
                if relative:x+=current[0];y+=current[1]
            current=(x,y);explicitly_closed=False
        output.append(segment)
    if start is not None and not explicitly_closed:
        output.append("z")
    return " ".join(output)


def _join_linear_paths(entries):
    """Join and simplify only linear paths; SVG arcs and curves remain native."""
    unused=set(range(len(entries))); result=[]
    while unused:
        index=min(unused); unused.remove(index)
        points=list(entries[index][1][0]); closed=entries[index][1][1]
        while not closed:
            found=None; reverse=False; prepend=False
            for other in sorted(unused):
                candidate=entries[other][1][0]
                if points[-1]==candidate[0]: found=other; break
                if points[-1]==candidate[-1]: found=other; reverse=True; break
                if candidate[-1]==points[0]: found=other; prepend=True; break
                if candidate[0]==points[0]: found=other; reverse=True; prepend=True; break
            if found is None: break
            unused.remove(found); segment=list(entries[found][1][0])
            if reverse: segment.reverse()
            if prepend: points=segment[:-1]+points
            else: points.extend(segment[1:])
            if len(points)>2 and points[0]==points[-1]:
                points.pop(); closed=True
        points=_simplify(points+[points[0]] if closed else points)
        if closed and points[-1]==points[0]: points=points[:-1]
        data="M %s %s "%points[0]+" ".join("L %s %s"%p for p in points[1:])
        if closed:data+=" Z"
        element=entries[index][0]
        element.set("d",data)
        result.append(element)
    return result


def _postprocess_svg(filename):
    """Join exact-endpoint straight paths in FreeCAD's flattened SVG output."""
    ET.register_namespace("","http://www.w3.org/2000/svg")
    tree=ET.parse(filename); root=tree.getroot()
    parents={child:parent for parent in root.iter() for child in parent}
    buckets={}
    for element in list(root.iter()):
        if not element.tag.endswith("path") or "d" not in element.attrib: continue
        if not _is_stroke_only(element): continue
        parent=parents.get(element)
        if parent is None or not parent.tag.endswith("g"): continue
        parsed=_line_path_points(element.attrib["d"])
        if parsed is None: continue
        key=(parent.attrib.get("transform",""),_line_style_key(element))
        buckets.setdefault(key,[]).append((element,parsed,parent))
    for entries in buckets.values():
        parents_used=[]
        for _,_,parent in entries:
            if parent not in parents_used: parents_used.append(parent)
        merged=_join_linear_paths([(element,parsed) for element,parsed,_ in entries])
        for element,_,parent in entries: parent.remove(element)
        target=parents_used[0]
        for element in merged: target.append(element)

    # Close every remaining path after endpoint joining, then consolidate path
    # elements with the same transform and visual style into one SVG element.
    parents={child:parent for parent in root.iter() for child in parent}
    path_buckets={}
    for element in root.iter():
        if element.tag.endswith("path") and "d" in element.attrib:
            element.set("d",_close_svg_subpaths(element.attrib["d"]))
            parent=parents.get(element)
            if parent is not None and parent.tag.endswith("g"):
                key=(parent.attrib.get("transform",""),_line_style_key(element))
                path_buckets.setdefault(key,[]).append((element,parent))
    for entries in path_buckets.values():
        if len(entries)<2: continue
        first=entries[0][0]
        first.set("d"," ".join(element.attrib["d"] for element,_ in entries))
        for element,parent in entries[1:]: parent.remove(element)
    tree.write(filename,encoding="utf-8",xml_declaration=True)


def _selection_boxes(selection):
    boxes=[]
    for obj in selection:
        shape=getattr(obj,"Shape",None)
        if shape is None: continue
        try:
            shape=shape.copy(); shape.Placement=obj.getGlobalPlacement()
        except Exception: pass
        try:
            b=shape.BoundBox; boxes.append((b.XMin,b.YMin,b.XMax,b.YMax))
        except Exception: pass
    return boxes


def _close_linear_subpaths_only(data):
    """Close only complete linear loops, never individual open fragments."""
    # A path can hold multiple M-delimited fragments. Closing each fragment
    # independently inserts spurious chords. Only add z where a simple
    # line-only subpath already returns to its own start; arc fragments remain
    # open until the joiner has assembled the complete contour.
    parts = re.split(r"(?=[Mm])", data)
    result = []
    for part in parts:
        if not part.strip():
            continue
        if re.search(r"(?<![A-Za-z])[Aa](?![A-Za-z])", part):
            result.append(part)
        else:
            points = _line_path_points(part)
            if points and len(points) > 2 and points[0] == points[-1]:
                result.append(_close_svg_subpaths(part))
            else:
                result.append(part)
    return " ".join(result) if result else data


def _close_svg_subpaths(data):
    """Defer closing until all line and arc fragments have been joined."""
    return data


def _force_closed_path_data(svg_path):
    """Ensure every exported path subpath ends in SVG's lowercase close command."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    open_paths = []
    split_outputs = []
    path_number = 0
    number_pattern = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?")
    # Absolute SVG user units. This only absorbs coordinate serialization
    # noise; it must not bridge visibly separated geometry.
    endpoint_tolerance = 1.0e-6
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "path":
            continue
        path_number += 1
        data = element.get("d")
        if not data:
            continue
        # Never let SVG's z invent a closing chord. A subpath is closed only
        # when its actual last point is already exactly its first point.
        parts = [part for part in re.split(r"(?=[Mm])", data) if part.strip()]
        closed_parts = []
        for part in parts:
            # Ignore any existing close command while checking geometry;
            # SVG Z itself may conceal a synthetic straight closing edge.
            geometry = re.sub(r"[Zz]\s*$", "", part).rstrip()
            nums = number_pattern.findall(geometry)
            if len(nums) < 4:
                open_paths.append(path_number)
                closed_parts.append(geometry)
                continue
            start = (float(nums[0]), float(nums[1]))
            end = (float(nums[-2]), float(nums[-1]))
            gap = math.hypot(end[0] - start[0], end[1] - start[1])
            has_arc = re.search(r"(?<![A-Za-z])[Aa](?![A-Za-z])", geometry) is not None
            # An arc fragment is allowed to remain open here: it may connect
            # to another existing line/arc fragment. Never snap or close an
            # individual arc, since doing so changes the arc endpoint.
            if has_arc and gap > endpoint_tolerance:
                closed_parts.append(geometry)
                continue
            if gap > endpoint_tolerance:
                open_paths.append(path_number)
                closed_parts.append(geometry)
                continue
            # Snap only the existing final endpoint, so z cannot introduce a
            # closing segment. Keep earlier geometry and circular arcs intact.
            matches = list(number_pattern.finditer(geometry))
            if gap > 0.0 and len(matches) >= 2:
                end_x, end_y = matches[-2:]
                geometry = (
                    geometry[:end_x.start()]
                    + format(start[0], ".12g")
                    + geometry[end_x.end():end_y.start()]
                    + format(start[1], ".12g")
                    + geometry[end_y.end():]
                )
            closed_parts.append(geometry.rstrip() + " z")
        element.set("d", " ".join(closed_parts))
        split_outputs.append((element, closed_parts))
    if open_paths:
        unique = ", ".join(str(n) for n in sorted(set(open_paths)))
        raise RuntimeError(
            _tr("ShaperExport: Kein geschlossener Pfad ohne neue Verbindungslinie möglich. Offene SVG-Pfade: ",
                "ShaperExport: Could not form a closed path without adding a connecting line. Open SVG paths: ") + unique
        )
    # Emit one SVG path element per M-delimited contour instead of leaving
    # several contours packed into a single d attribute.
    parents = {child: parent for parent in root.iter() for child in parent}
    for element, contours in split_outputs:
        if len(contours) <= 1:
            continue
        parent = parents.get(element)
        if parent is None:
            continue
        index = list(parent).index(element)
        parent.remove(element)
        for offset, contour in enumerate(contours):
            separate = ET.Element(element.tag, dict(element.attrib))
            separate.set("d", contour)
            parent.insert(index + offset, separate)
    tree.write(svg_path, encoding="utf-8", xml_declaration=True)


def _force_closed_path_data(svg_path):
    """Join line/arc fragments and express circles as explicitly closed paths."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    parents = {child: parent for parent in root.iter() for child in parent}
    number_re = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?")
    token_re = re.compile(r"([MmLlAaZz])|([-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?)")
    # FreeCAD serializes arc endpoints to fewer decimal places than many line
    # endpoints. Allow up to 0.001 SVG units for endpoint matching.
    tolerance = 0.001

    def canonical_path(data):
        return tuple(cmd if cmd else format(round(float(num), 6), ".6g")
                     for cmd, num in token_re.findall(data))

    def close(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1]) <= tolerance

    def parse_fragment(data, element):
        tokens = token_re.findall(data)
        values = [(cmd if cmd else float(num)) for cmd, num in tokens]
        if len(values) < 6 or values[0] not in ("M",):
            raise RuntimeError(_tr("Nicht unterstützte SVG-Pfadsyntax", "Unsupported SVG path syntax"))
        start = (float(values[1]), float(values[2]))
        command = values[3]
        if command == "L" and len(values) == 6:
            end = (float(values[4]), float(values[5]))
            forward = "M {} {} L {} {}".format(*start, *end)
            reverse = "M {} {} L {} {}".format(*end, *start)
        elif command == "A" and len(values) == 11:
            args = values[4:11]
            # SVG arc flags have a strict 0/1 grammar; serializing them as
            # floating-point values (0.0/1.0) makes the entire d invalid.
            args[3] = int(args[3])  # large-arc flag
            args[4] = int(args[4])  # sweep flag
            end = (float(args[5]), float(args[6]))
            forward = "M {} {} A {} {} {} {} {} {} {}".format(*start, *args)
            reverse_args = args[:4] + [1 - int(args[4]), start[0], start[1]]
            reverse = "M {} {} A {} {} {} {} {} {} {}".format(*end, *reverse_args)
        else:
            raise RuntimeError(_tr("Nicht unterstützte SVG-Pfadsyntax", "Unsupported SVG path syntax"))
        return {"start": start, "end": end, "fwd": forward, "rev": reverse,
                "signature": min(canonical_path(forward), canonical_path(reverse)),
                "element": element}

    def segments(data, element):
        chunks = [p.strip() for p in re.split(r"(?=[Mm])", data) if p.strip()]
        return [parse_fragment(chunk, element) for chunk in chunks]

    # Keep style and parent context intact while allowing fragments from
    # separate path elements to join.
    grouped = {}
    for element in list(root.iter()):
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "circle" and all(element.get(key) is not None
                                   for key in ("cx", "cy", "r")):
            cx, cy, radius = (float(element.get(key))
                              for key in ("cx", "cy", "r"))
            if radius <= 0:
                continue
            # Four quarter-circle arcs match the format used by Shaper Origin
            # files while preserving the analytic circle geometry.
            d = ("M {right:.12g} {cy:.12g} "
                 "A {r:.12g} {r:.12g} 0 0 1 {cx:.12g} {bottom:.12g} "
                 "A {r:.12g} {r:.12g} 0 0 1 {left:.12g} {cy:.12g} "
                 "A {r:.12g} {r:.12g} 0 0 1 {cx:.12g} {top:.12g} "
                 "A {r:.12g} {r:.12g} 0 0 1 {right:.12g} {cy:.12g} z").format(
                     right=cx + radius, left=cx - radius,
                     top=cy - radius, bottom=cy + radius,
                     cx=cx, cy=cy, r=radius)
            element.tag = (element.tag.rsplit("}", 1)[0] + "}path"
                           if "}" in element.tag else "path")
            element.attrib.pop("cx", None)
            element.attrib.pop("cy", None)
            element.attrib.pop("r", None)
            element.set("d", d)
            element.set("data-shaper-circle", "true")
            continue
        if tag != "path" or not element.get("d"):
            continue
        parent = parents.get(element)
        if parent is None:
            continue
        style = tuple(sorted((k, v) for k, v in element.attrib.items()
                             if k not in ("id", "d")))
        # Segments from overlapping selected objects may be stored in
        # different SVG subgroups. Deduplicate and stitch across those groups.
        key = style
        try:
            found = segments(element.get("d"), element)
        except Exception as exc:
            raise RuntimeError(_tr("ShaperExport: Pfad {} kann nicht verbunden werden: {}",
                                   "ShaperExport: Could not join path {}: {}").format(
                element.get("id", "ohne ID"), exc))
        grouped.setdefault(key, {"parent": parent, "style": style, "pieces": [], "elements": []})
        grouped[key]["pieces"].extend(found)
        grouped[key]["elements"].append(element)

    for group in grouped.values():
        pending = []
        seen_segments = set()
        for piece in group["pieces"]:
            if piece["signature"] in seen_segments:
                continue
            seen_segments.add(piece["signature"])
            pending.append(piece)
        contours = []
        while pending:
            first = pending[0]
            furthest = {"steps": [], "end": None, "remaining": []}

            def find_cycle(start, end, remaining, steps):
                if len(steps) > len(furthest["steps"]):
                    furthest["steps"] = list(steps)
                    furthest["end"] = end
                    furthest["remaining"] = list(remaining)
                if close(end, start):
                    return steps
                for i, piece in enumerate(remaining):
                    orientations = []
                    if close(piece["start"], end):
                        orientations.append((False, piece["end"]))
                    if close(piece["end"], end):
                        orientations.append((True, piece["start"]))
                    for reverse_piece, next_end in orientations:
                        result = find_cycle(
                            start, next_end, remaining[:i] + remaining[i + 1:],
                            steps + [(piece, reverse_piece)])
                        if result is not None:
                            return result
                return None

            cycle = find_cycle(first["start"], first["end"], pending[1:], [(first, False)])
            if cycle is None:
                cycle = find_cycle(first["end"], first["start"], pending[1:], [(first, True)])
            closed = cycle is not None
            if not closed:
                # Shaper accepts open contours as on-line or guide cuts. Keep
                # the longest chain assembled from existing segments; do not
                # add a closing edge or snap its final endpoint to the start.
                cycle = furthest["steps"]
                if not cycle:
                    cycle = [(first, False)]

            start = cycle[0][0]["end"] if cycle[0][1] else cycle[0][0]["start"]
            commands = ""
            used_pieces = []
            for piece, reversed_piece in cycle:
                command_data = piece["rev"] if reversed_piece else piece["fwd"]
                command_data = re.sub(r"^M\s*[-+\d.eE]+\s+[-+\d.eE]+\s*", "", command_data)
                commands += (" " if commands else "") + command_data
                used_pieces.append(piece)
            used_ids = {id(piece) for piece in used_pieces}
            pending = [piece for piece in pending if id(piece) not in used_ids]
            contour = "M {} {} {}".format(start[0], start[1], commands)
            if closed:
                # Snap the existing final endpoint within tolerance, so z
                # cannot introduce a new closing edge.
                matches = list(number_re.finditer(contour))
                if len(matches) >= 2:
                    mx, my = matches[-2:]
                    contour = (contour[:mx.start()] + format(start[0], ".12g")
                               + contour[mx.end():my.start()] + format(start[1], ".12g")
                               + contour[my.end():])
                contours.append(contour + " z")
            else:
                contours.append(contour)

        parent = group["parent"]
        elements = group["elements"]
        local_elements = [el for el in elements if parents.get(el) is parent]
        insert_at = min((list(parent).index(el) for el in local_elements), default=len(list(parent)))
        template = elements[0]
        for el in elements:
            actual_parent = parents.get(el)
            if actual_parent is not None:
                actual_parent.remove(el)
        for offset, contour in enumerate(contours):
            separate = ET.Element(template.tag, dict(template.attrib))
            separate.set("d", contour)
            parent.insert(insert_at + offset, separate)

    # Shaper Origin uses visible RGB geometry with real-world stroke units.
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] not in (
                "path", "circle", "line", "polyline", "polygon", "ellipse", "rect"):
            continue
        element.set("stroke", "#000000")
        element.set("fill", "#000000")
        element.set("stroke-width", "0.01")
        style = element.get("style")
        declarations = {"stroke": "#000000", "fill": "#000000",
                        "stroke-width": "0.01"}
        style = style or ""
        for property_name, value in declarations.items():
            pattern = r"(^|;)\s*(" + re.escape(property_name) + r")\s*:\s*[^;]*"
            if re.search(pattern, style, re.IGNORECASE):
                style = re.sub(pattern, r"\1" + property_name + ":" + value,
                               style, flags=re.IGNORECASE)
            else:
                style = style.rstrip(";") + ";" + property_name + ":" + value
        element.set("style", style.lstrip(";"))

    tree.write(svg_path, encoding="utf-8", xml_declaration=True)


def _postprocess_svg(svg_path):
    """Keep FreeCAD's original path elements intact until topology is joined."""
    return


def _sample_contour(data, arc_steps=16):
    """Flatten an absolute M/L/A contour for containment checks."""
    tokens = re.findall(r"[MLAZz]|[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?", data)
    points = []
    current = start = None
    i = 0
    try:
        while i < len(tokens):
            command = tokens[i]
            i += 1
            if command == "M":
                current = (float(tokens[i]), float(tokens[i + 1])); i += 2
                start = current
                points.append(current)
            elif command == "L":
                current = (float(tokens[i]), float(tokens[i + 1])); i += 2
                points.append(current)
            elif command == "A":
                rx, ry, rotation = map(float, tokens[i:i + 3])
                large, sweep = int(tokens[i + 3]), int(tokens[i + 4])
                end = (float(tokens[i + 5]), float(tokens[i + 6])); i += 7
                x1, y1 = current; x2, y2 = end
                phi = math.radians(rotation); cp, sp = math.cos(phi), math.sin(phi)
                rx, ry = abs(rx), abs(ry)
                if rx == 0 or ry == 0 or current == end:
                    points.append(end)
                else:
                    dx, dy = (x1-x2)/2, (y1-y2)/2
                    xp, yp = cp*dx+sp*dy, -sp*dx+cp*dy
                    scale = max(1.0, xp*xp/(rx*rx)+yp*yp/(ry*ry))
                    rx *= math.sqrt(scale); ry *= math.sqrt(scale)
                    numerator = max(0.0, rx*rx*ry*ry-rx*rx*yp*yp-ry*ry*xp*xp)
                    denominator = rx*rx*yp*yp+ry*ry*xp*xp
                    factor = (1 if large != sweep else -1) * math.sqrt(numerator/denominator) if denominator else 0
                    cxp, cyp = factor*rx*yp/ry, factor*-ry*xp/rx
                    cx, cy = cp*cxp-sp*cyp+(x1+x2)/2, sp*cxp+cp*cyp+(y1+y2)/2
                    a0 = math.atan2((yp-cyp)/ry, (xp-cxp)/rx)
                    da = math.atan2((-yp-cyp)/ry, (-xp-cxp)/rx)-a0
                    if sweep and da < 0: da += 2*math.pi
                    if not sweep and da > 0: da -= 2*math.pi
                    count = max(1, int(math.ceil(abs(da)/(2*math.pi)*arc_steps)))
                    for step in range(1, count+1):
                        angle = a0+da*step/count
                        points.append((cx+cp*rx*math.cos(angle)-sp*ry*math.sin(angle),
                                       cy+sp*rx*math.cos(angle)+cp*ry*math.sin(angle)))
                current = end
            elif command in ("Z", "z"):
                if current != start: points.append(start)
                current = start
            else:
                return []
    except (ValueError, IndexError, ZeroDivisionError):
        return []
    return points if len(points) >= 4 and points[0] == points[-1] else []


def _point_in_contour(point, polygon):
    x, y = point
    inside = False
    for a, b in zip(polygon, polygon[1:]):
        if (a[1] > y) != (b[1] > y):
            crossing = (b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]
            if x < crossing: inside = not inside
    return inside


def _set_svg_style(element, declarations):
    for name, value in declarations.items():
        element.set(name, value)
    style = element.get("style", "")
    for name, value in declarations.items():
        pattern = r"(^|;)\s*" + re.escape(name) + r"\s*:\s*[^;]*"
        if re.search(pattern, style, re.IGNORECASE):
            style = re.sub(pattern, r"\1" + name + ":" + value,
                           style, flags=re.IGNORECASE)
        else:
            style = style.rstrip(";") + ";" + name + ":" + value
    element.set("style", style.lstrip(";"))


def _classify_contours_by_size(svg_path, max_pocket_size):
    """Classify closed contours by size while keeping outer contours black."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    contours = []
    parents = {child: parent for parent in root.iter() for child in parent}
    for element in list(root.iter()):
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "path" and element.get("d"):
            # FreeCAD can consolidate multiple subpaths into one SVG path. Split
            # them so that every physical contour can receive its own size class.
            subpaths = [part.strip() for part in re.split(r"(?=[Mm])", element.get("d")) if part.strip()]
            closed = [(part, _sample_contour(part, arc_steps=128)) for part in subpaths]
            closed = [(part, polygon) for part, polygon in closed if polygon]
            if closed:
                parent = parents.get(element)
                insert_at = list(parent).index(element) if parent is not None else 0
                open_parts = [part for part in subpaths if not _sample_contour(part)]
                output_elements = []
                for part, polygon in closed:
                    contour = ET.Element(element.tag, dict(element.attrib))
                    contour.set("d", part)
                    bounds = (min(point[0] for point in polygon), min(point[1] for point in polygon),
                              max(point[0] for point in polygon), max(point[1] for point in polygon))
                    contours.append((contour, bounds, polygon))
                    output_elements.append(contour)
                if open_parts:
                    open_element = ET.Element(element.tag, dict(element.attrib))
                    open_element.set("d", " ".join(open_parts))
                    output_elements.append(open_element)
                if parent is not None:
                    parent.remove(element)
                    for offset, output_element in enumerate(output_elements):
                        parent.insert(insert_at + offset, output_element)
            else:
                continue
        elif tag == "circle" and all(element.get(key) is not None for key in ("cx", "cy", "r")):
            cx, cy, radius = (float(element.get(key)) for key in ("cx", "cy", "r"))
            if radius > 0:
                polygon = [(cx+radius*math.cos(2*math.pi*i/64),
                            cy+radius*math.sin(2*math.pi*i/64)) for i in range(65)]
                contours.append((element, (cx-radius, cy-radius, cx+radius, cy+radius), polygon))
    for element, bounds, polygon in contours:
        width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        is_pocket = width <= max_pocket_size and height <= max_pocket_size
        nested = any(other is not element and _point_in_contour(polygon[0], other_polygon)
                     for other, _, other_polygon in contours)
        if is_pocket:
            declarations = (("fill", "#777777"), ("stroke", "none"),
                            ("stroke-width", "0.01"))
        elif nested:
            declarations = (("fill", "#ffffff"), ("stroke", "#000000"), ("stroke-width", "0.01"))
        else:
            declarations = (("fill", "#000000"), ("stroke", "#000000"), ("stroke-width", "0.01"))
        _set_svg_style(element, dict(declarations + (
            ("opacity", "1"), ("fill-opacity", "1"), ("stroke-opacity", "1"))))
    tree.write(svg_path, encoding="utf-8", xml_declaration=True)


def _classify_open_paths(svg_path):
    """Encode open contours as gray on-line cuts, as Origin permits."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    declarations = {"fill": "none", "stroke": "#777777",
                    "stroke-width": "0.01", "opacity": "1",
                    "fill-opacity": "1", "stroke-opacity": "1"}
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "path" or not element.get("d"):
            continue
        parts = [part.strip() for part in re.split(r"(?=[Mm])", element.get("d"))
                 if part.strip()]
        if parts and not any(_sample_contour(part) for part in parts):
            _set_svg_style(element, declarations)
    tree.write(svg_path, encoding="utf-8", xml_declaration=True)


def _normalize_shaper_opacity(svg_path):
    """Remove inherited transparency so RGB cut colors stay unambiguous."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    declarations = {"opacity": "1", "fill-opacity": "1", "stroke-opacity": "1"}
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in ("svg", "g", "path", "circle", "line", "polyline",
                   "polygon", "ellipse", "rect"):
            _set_svg_style(element, declarations)
    tree.write(svg_path, encoding="utf-8", xml_declaration=True)


def _normalize_contour_ids(svg_path):
    """Use stable pathN/circleN IDs while preserving Draft outer contour IDs."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    elements = [element for element in root.iter()
                if element.tag.rsplit("}", 1)[-1] in ("path", "circle")]
    reserved = {element.get("id") for element in root.iter()
                if element.get("id") and element not in elements}
    preserved = set(reserved)

    # Keep the IDs that identify the outer Draft contour and converted circles.
    for element in elements:
        element_id = element.get("id", "")
        if (re.fullmatch(r"Shape2DView_nwe\d+", element_id)
                or re.fullmatch(r"circle\d+", element_id)) and element_id not in preserved:
            preserved.add(element_id)
        else:
            element.attrib.pop("id", None)

    next_number = {"path": 1, "circle": 1}
    for element in elements:
        if element.get("id"):
            continue
        prefix = ("circle" if element.get("data-shaper-circle") == "true"
                  or re.fullmatch(r"circle\d+", element.get("id", ""))
                  else "path")
        number = next_number[prefix]
        while "{}{}".format(prefix, number) in preserved:
            number += 1
        element.set("id", "{}{}".format(prefix, number))
        preserved.add(element.get("id"))
        next_number[prefix] = number + 1
        element.attrib.pop("data-shaper-circle", None)

    for element in elements:
        element.attrib.pop("data-shaper-circle", None)

    tree.write(svg_path, encoding="utf-8", xml_declaration=True)


def _pretty_svg_xml(root):
    """Serialize SVG with one attribute per line and readable indentation."""
    preferred_prefixes = {
        "http://www.w3.org/2000/svg": "",
        "http://www.w3.org/1999/xlink": "xlink",
        "http://www.inkscape.org/namespaces/inkscape": "inkscape",
        "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd": "sodipodi",
    }
    uris = set()
    for element in root.iter():
        for name in (element.tag,) + tuple(element.attrib):
            if name.startswith("{") and "}" in name:
                uris.add(name[1:].split("}", 1)[0])
    namespaces = {}
    used_prefixes = set()
    for uri in sorted(uris):
        prefix = preferred_prefixes.get(uri)
        if prefix is None:
            number = 0
            while "ns{}".format(number) in used_prefixes:
                number += 1
            prefix = "ns{}".format(number)
        if prefix:
            used_prefixes.add(prefix)
        namespaces[uri] = prefix

    def qualified(name):
        if name.startswith("{") and "}" in name:
            uri, local = name[1:].split("}", 1)
            prefix = namespaces.get(uri, "")
            return "{}:{}".format(prefix, local) if prefix else local
        return name

    def render(element, depth):
        indentation = "  " * depth
        tag = qualified(element.tag)
        attributes = []
        if depth == 0:
            for uri, prefix in sorted(namespaces.items(), key=lambda item: item[1]):
                declaration = "xmlns" if not prefix else "xmlns:" + prefix
                attributes.append((declaration, uri))
        attributes.extend((qualified(name), value)
                          for name, value in element.attrib.items())
        opening = indentation + "<" + tag
        if attributes:
            opening += "\n" + "\n".join(
                "  " * (depth + 1) + name + "=" + quoteattr(value)
                for name, value in attributes)
        children = list(element)
        text = element.text or ""
        if not children and not text.strip():
            return [opening + " />"]
        if not children:
            return [opening + ">" + escape(text) + "</" + tag + ">"]
        lines = [opening + ">"]
        if text.strip():
            lines.append("  " * (depth + 1) + escape(text))
        for child in children:
            lines.extend(render(child, depth + 1))
            if child.tail and child.tail.strip():
                lines.append("  " * (depth + 1) + escape(child.tail))
        lines.append(indentation + "</" + tag + ">")
        return lines

    return '<?xml version="1.0" encoding="utf-8"?>\n' + "\n".join(render(root, 0)) + "\n"


def _pocket_size_dialog(parent):
    """Ask for and persist the shared maximum pocket width and height in mm."""
    try:
        from PySide import QtWidgets
    except ImportError:
        try:
            from PySide2 import QtWidgets
        except ImportError:
            from PySide6 import QtWidgets
    pref = App.ParamGet("User parameter:BaseApp/Preferences/Mod/ShaperExport")
    current = pref.GetFloat("max_pocket_size_mm", 20.0)
    value, accepted = QtWidgets.QInputDialog.getDouble(
        parent,
        _tr("Taschengröße", "Pocket size"),
        _tr("Maximale Breite und Höhe (mm):", "Maximum width and height (mm):"),
        current, 0.001, 1000000.0, 2)
    if accepted:
        pref.SetFloat("max_pocket_size_mm", value)
        return value
    return None


def _combine_contour_paths(svg_path):
    """Apply descendant transforms and combine all contours like Inkscape."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    parents = {child: parent for parent in root.iter() for child in parent}
    shapes = [element for element in root.iter()
              if (element.tag.rsplit("}", 1)[-1] == "path" and element.get("d"))
              or (element.tag.rsplit("}", 1)[-1] == "circle"
                  and all(element.get(k) is not None for k in ("cx", "cy", "r")))]
    if len(shapes) < 2:
        tree.write(svg_path, encoding="utf-8", xml_declaration=True)
        return

    chains = []
    for shape in shapes:
        chain = [shape]
        current = parents.get(shape)
        while current is not None:
            chain.append(current)
            current = parents.get(current)
        chains.append(chain)
    common_parent = next((element for element in root.iter()
                          if element.tag.rsplit("}", 1)[-1] == "g"
                          and element.get("id") == "Shape2DView"), None)
    if common_parent is None:
        common_parent = next((candidate for candidate in chains[0]
                              if all(candidate in chain for chain in chains[1:])), root)

    def multiply(left, right):
        a,b,c,d,e,f = left
        g,h,i,j,k,l = right
        return (a*g+c*h, b*g+d*h, a*i+c*j, b*i+d*j,
                a*k+c*l+e, b*k+d*l+f)

    def parse_transform(value):
        result = (1.,0.,0.,1.,0.,0.)
        transforms = re.findall(r"(translate|scale|matrix|rotate)\s*\(([^)]*)\)", value or "")
        if value and re.sub(r"(?:translate|scale|matrix|rotate)\s*\([^)]*\)", "", value).strip():
            raise RuntimeError(_tr("ShaperExport: Nicht unterstützte SVG-Transformation: ",
                                   "ShaperExport: Unsupported SVG transform: ") + value)
        for name, raw in transforms:
            values = [float(v) for v in re.findall(
                r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?", raw)]
            if name == "translate":
                op = (1.,0.,0.,1.,values[0],values[1] if len(values)>1 else 0.)
            elif name == "scale":
                op = (values[0],0.,0.,values[1] if len(values)>1 else values[0],0.,0.)
            elif name == "rotate":
                angle = math.radians(values[0])
                cosine, sine = math.cos(angle), math.sin(angle)
                rotation = (cosine, sine, -sine, cosine, 0., 0.)
                if len(values) >= 3:
                    cx, cy = values[1], values[2]
                    rotation = multiply((1.,0.,0.,1.,cx,cy),
                                        multiply(rotation,(1.,0.,0.,1.,-cx,-cy)))
                op = rotation
            else:
                op = tuple(values[:6])
            result = multiply(result, op)
        return result

    def transform_path_data(data, matrix):
        a,b,c,d,e,f = matrix
        # FreeCAD path geometry uses absolute M/L/A commands. Keep arc
        # geometry analytic; accept translations and axis-aligned uniform
        # scale/reflection, the transform forms used by Draft Shaper export.
        sx = math.hypot(a, b)
        sy = math.hypot(c, d)
        orthogonal = abs(a*c+b*d) <= 1e-9
        if not orthogonal or abs(sx-sy) > 1e-9:
            raise RuntimeError(_tr(
                "ShaperExport: Inkscape-Kombinieren unterstützt hier nur Translation und gleichmäßige Skalierung/Spiegelung.",
                "ShaperExport: Inkscape combining here supports only translation and uniform scale/reflection."))
        scale = sx
        angle = math.degrees(math.atan2(b, a))
        reflected = (a*d-b*c) < 0
        token_re = re.compile(r"([MLAaZz])|([-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?)")
        tokens = [cmd if cmd else float(num) for cmd, num in token_re.findall(data)]
        out = []
        index = 0
        def point(x, y):
            return (a*x+c*y+e, b*x+d*y+f)
        while index < len(tokens):
            command = tokens[index]
            index += 1
            if command in ("M", "L"):
                x, y = point(float(tokens[index]), float(tokens[index+1]))
                index += 2
                out.append("{} {:.12g} {:.12g}".format(command, x, y))
            elif command in ("A", "a"):
                rx, ry, rotation = map(float, tokens[index:index+3])
                large, sweep = int(tokens[index+3]), int(tokens[index+4])
                x, y = point(float(tokens[index+5]), float(tokens[index+6]))
                index += 7
                out.append("A {:.12g} {:.12g} {:.12g} {} {} {:.12g} {:.12g}".format(
                    abs(rx*scale), abs(ry*scale), rotation + angle, large,
                    1-sweep if reflected else sweep, x, y))
            elif command in ("Z", "z"):
                out.append("z")
            else:
                raise RuntimeError(_tr("ShaperExport: Unerwarteter SVG-Pfadbefehl beim Kombinieren: {}",
                                       "ShaperExport: Unexpected SVG path command while combining: {}").format(command))
        return " ".join(out)

    combined_parts = []
    seen_circles = set()
    for shape, chain in zip(shapes, chains):
        # Keep FreeCAD's exported coordinates and the shared Shape2DView
        # transform intact. Applying these transforms again displaced geometry.
        matrix = (1.,0.,0.,1.,0.,0.)
        if shape.tag.rsplit("}", 1)[-1] == "path":
            combined_parts.append(transform_path_data(shape.get("d", ""), matrix))
        else:
            cx, cy, radius = (float(shape.get(k)) for k in ("cx", "cy", "r"))
            circle_key = (round(cx, 6), round(cy, 6), round(radius, 6))
            if circle_key in seen_circles:
                continue
            seen_circles.add(circle_key)
            circle_data = ("M {right} {cy} A {r} {r} 0 0 1 {cx} {bottom} "
                           "A {r} {r} 0 0 1 {left} {cy} "
                           "A {r} {r} 0 0 1 {cx} {top} "
                           "A {r} {r} 0 0 1 {right} {cy} z").format(
                               right=cx+radius, left=cx-radius,
                               cx=cx, cy=cy, r=radius,
                               bottom=cy+radius, top=cy-radius)
            combined_parts.append(transform_path_data(circle_data, matrix))

    path_templates = [shape for shape in shapes if shape.tag.rsplit("}", 1)[-1] == "path"]
    template = path_templates[0] if path_templates else shapes[0]
    destination = common_parent if common_parent is not None else root
    template_chain = chains[shapes.index(template)]
    common_index = template_chain.index(destination) if destination in template_chain else len(template_chain)-1
    insertion_child = template_chain[common_index-1] if common_index > 0 else template
    insertion_index = (list(destination).index(insertion_child)
                       if insertion_child in list(destination) else len(list(destination)))
    path_tag = template.tag.rsplit("}", 1)[0] + "}path" if "}" in template.tag else "path"
    combined = ET.Element(path_tag, dict(template.attrib))
    for attribute in ("cx", "cy", "r"):
        combined.attrib.pop(attribute, None)
    combined.set("d", " ".join(combined_parts))
    for shape in shapes:
        parent = parents.get(shape)
        if parent is not None:
            parent.remove(shape)
    destination.insert(insertion_index, combined)

    # Remove now-empty wrapper groups so Inkscape sees the combined path as a
    # path object instead of a group that still needs ungrouping.
    changed = True
    while changed:
        changed = False
        for element in list(root.iter()):
            if element is root or element.tag.rsplit("}", 1)[-1] != "g":
                continue
            if not list(element) and not (element.text or "").strip():
                parent = parents.get(element)
                if parent is not None:
                    parent.remove(element)
                    changed = True
    tree.write(svg_path, encoding="utf-8", xml_declaration=True)


def export_selection():
    selection=Gui.Selection.getSelection()
    if not selection:
        App.Console.PrintError("ShaperExport: %s\n" % _tr("Bitte Objekte auswählen.", "Please select objects.")); return
    boxes=_selection_boxes(selection)
    if not boxes:
        App.Console.PrintError("ShaperExport: %s\n" % _tr("Keine exportierbaren Shapes gefunden.", "No exportable shapes found.")); return
    try:
        try:
            from PySide import QtWidgets
        except ImportError:
            from PySide6 import QtWidgets
        max_pocket_size = _pocket_size_dialog(Gui.getMainWindow())
        if max_pocket_size is None: return
        document=App.activeDocument()
        current_file=getattr(document,"FileName","")
        start_path=(os.path.splitext(current_file)[0]+".svg" if current_file else
                    os.path.join(os.path.expanduser("~"),document.Name+".svg"))
        result=QtWidgets.QFileDialog.getSaveFileName(
            Gui.getMainWindow(),_tr("SVG-Datei speichern", "Save SVG file"),start_path,
            _tr("SVG-Dateien (*.svg)", "SVG files (*.svg)"))
        filename=result[0] if isinstance(result,tuple) else result
    except Exception as exc:
        App.Console.PrintError("ShaperExport: %s\n" % _tr(
            "Dateidialog konnte nicht geöffnet werden: %s" % exc,
            "Could not open file dialog: %s" % exc))
        return
    if not filename: return
    if not filename.lower().endswith(".svg"): filename+=".svg"
    temp_fd,temp_path=tempfile.mkstemp(suffix=".svg")
    os.close(temp_fd)
    pref=App.ParamGet("User parameter:BaseApp/Preferences/Mod/Draft")
    pref_contents=pref.GetContents()
    had_export_style=any(item[0]=="shaper_export_style" for item in pref_contents)
    old_export_style=pref.GetInt("shaper_export_style",0)
    try:
        import importSVG
        pref.SetInt("shaper_export_style",1)  # raw coordinates allow endpoint matching across objects
        try:
            importSVG.export(selection,temp_path.replace("\\","/"))
        finally:
            if had_export_style: pref.SetInt("shaper_export_style",old_export_style)
            else: pref.RemInt("shaper_export_style")
        if not os.path.exists(temp_path) or os.path.getsize(temp_path)==0:
            raise RuntimeError(_tr("FreeCADs Shaper-Exporter hat keine SVG-Datei erzeugt",
                                   "FreeCAD's Shaper exporter did not create an SVG file"))
        _postprocess_svg(temp_path)
        _force_closed_path_data(temp_path)
        _classify_contours_by_size(temp_path, max_pocket_size)
        _classify_open_paths(temp_path)
        _normalize_shaper_opacity(temp_path)
        _normalize_contour_ids(temp_path)
        formatted_tree = ET.parse(temp_path)
        for node in formatted_tree.getroot().iter():
            if node.text is not None and not node.text.strip():
                node.text = None
            if node.tail is not None and not node.tail.strip():
                node.tail = None
        with open(temp_path, "wb") as target:
            target.write(_pretty_svg_xml(formatted_tree.getroot()).encode("utf-8"))
        with open(temp_path,"rb") as source,open(filename,"wb") as target:
            target.write(source.read())
        final_root=ET.parse(filename).getroot()
        contour_count=sum(
            len(re.findall(r"(?<![A-Za-z])[Mm]", element.get("d", "")))
            if element.tag.rsplit("}",1)[-1]=="path" else 1
            for element in final_root.iter()
            if element.tag.rsplit("}",1)[-1] in ("path","circle"))
    except Exception as exc:
        App.Console.PrintError("%s\n" % _tr("ShaperExport: Export fehlgeschlagen: %s" % exc,
                                              "ShaperExport: Export failed: %s" % exc)); return
    finally:
        try: os.remove(temp_path)
        except OSError: pass
    count=_overlaps(boxes)
    msg=_tr("%d Konturen exportiert; %d Objektpaare überlappen in der XY-Begrenzungsbox.",
            "%d contours exported; %d object pairs overlap in their XY bounding boxes.") % (contour_count,count)
    App.Console.PrintMessage("ShaperExport: "+msg+"\n")
    try:
        try:
            from PySide import QtWidgets
        except ImportError:
            from PySide6 import QtWidgets
        QtWidgets.QMessageBox.information(Gui.getMainWindow(),_tr("Shaper-Export", "Shaper Export"),msg)
    except Exception: pass
