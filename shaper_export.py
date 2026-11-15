import os
import math
import re
import tempfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr
import FreeCAD as App
import FreeCADGui as Gui
from translations import translate

# SVG coordinates are exported in millimetres. Keep separate geometric and display settings.
ENDPOINT_TOLERANCE = 0.001
CONTOUR_SAMPLE_STEPS = 64
MAX_ARC_SAGITTA = 0.0001  # mm; polygon approximation error target
MAX_ARC_SEGMENTS = 16384
BOUNDARY_TOLERANCE = 1e-9  # mm, independent of endpoint joining tolerance


def _common_svg_scale(root):
    """Return absolute rendered X/Y scale; fail closed on unsupported transforms.

    The FreeCAD exporter uses a shared translate(...) scale(1,-1) group.
    Only common axis-aligned translate/scale transforms are accepted: rotations,
    skew and matrices must be flattened by the exporter before classification.
    """
    parents = {child: parent for parent in root.iter() for child in parent}
    paths = [e for e in root.iter() if e.tag.rsplit('}', 1)[-1] in ('path', 'circle')]
    chains = []
    for e in paths:
        chain = []
        node = e
        while node is not None:
            if node.get('transform'):
                chain.append(node.get('transform'))
            node = parents.get(node)
        chains.append(tuple(reversed(chain)))
    if len(set(chains)) > 1:
        raise RuntimeError('Different SVG transform contexts; flatten transforms before export')
    sx = sy = 1.0
    for transform in (chains[0] if chains else ()):
        pos = 0
        pattern = re.compile(r'\s*(translate|scale)\s*\(([^()]*)\)\s*')
        while pos < len(transform):
            match = pattern.match(transform, pos)
            if not match:
                raise RuntimeError('Unsupported SVG transform {!r}; flatten before export'.format(transform))
            values = [float(v) for v in re.split(r'[\s,]+', match.group(2).strip()) if v]
            if not values or not all(math.isfinite(v) for v in values) or len(values) > 2:
                raise RuntimeError('Invalid SVG transform {!r}'.format(transform))
            if match.group(1) == 'scale':
                sx *= values[0]
                sy *= values[1] if len(values) == 2 else values[0]
            pos = match.end()
    if min(abs(sx), abs(sy)) <= 0:
        raise RuntimeError('Non-invertible SVG scale')
    return abs(sx), abs(sy)

def _path_statistics(svg_root):
    """Count exported path subpaths and measure gaps on open paths."""
    number_pattern = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?")
    path_count = closed_count = 0
    open_gaps = []
    for element in svg_root.iter():
        if element.tag.rsplit("}", 1)[-1] != "path":
            continue
        path_id = element.get("id") or translate("(no ID)")
        parts = [part.strip() for part in re.split(r"(?=[Mm])", element.get("d", ""))
                 if part.strip()]
        for part in parts:
            path_count += 1
            _sample_contour(part)  # Validate before reporting statistics.
            is_closed = re.search(r"[Zz]\s*$", part) is not None
            if is_closed:
                closed_count += 1
            numbers = number_pattern.findall(part)
            if len(numbers) >= 4:
                start = (float(numbers[0]), float(numbers[1]))
                end = (float(numbers[-2]), float(numbers[-1]))
                if not is_closed:
                    open_gaps.append((path_id, math.hypot(end[0] - start[0], end[1] - start[1])))
            elif not is_closed:
                open_gaps.append((path_id, 0.0))
    return path_count, closed_count, open_gaps

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


def _force_closed_path_data(svg_path):
    """Join line/arc fragments and express circles as explicitly closed paths."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    parents = {child: parent for parent in root.iter() for child in parent}
    number_re = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?")
    token_re = re.compile(r"([MmLlAaZz])|([-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?)")
    # FreeCAD serializes arc endpoints to fewer decimal places than many line
    # endpoints. Allow up to 0.001 SVG units for endpoint matching.
    tolerance = ENDPOINT_TOLERANCE

    def close(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1]) <= tolerance

    def parse_fragment(data, element):
        """Parse a complete absolute SVG subpath containing M/L/A/Z commands."""
        tokens = re.findall(r"[MLAZz]|[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?", data)
        if "".join(tokens) != re.sub(r"[\s,]+", "", data):
            raise RuntimeError("Malformed or unsupported fragment")
        pieces = []
        i = 0
        current = start = None
        last_was_close = False

        def add_line(a, b):
            if a == b:
                return
            pieces.append({"start": a, "end": b,
                           "fwd": "M {:.12g} {:.12g} L {:.12g} {:.12g}".format(*a, *b),
                           "rev": "M {:.12g} {:.12g} L {:.12g} {:.12g}".format(*b, *a),
                           "geometry": ("L",), "element": element})

        while i < len(tokens):
            command = tokens[i]
            if command not in ("M", "L", "A", "Z", "z"):
                raise RuntimeError("Unsupported SVG path command {!r}".format(command))
            i += 1
            if command == "M":
                if i + 1 >= len(tokens) or tokens[i] in ("M", "L", "A", "Z", "z") or tokens[i+1] in ("M", "L", "A", "Z", "z"):
                    raise RuntimeError("Incomplete SVG move command")
                current = (float(tokens[i]), float(tokens[i+1])); i += 2
                if not all(math.isfinite(v) for v in current):
                    raise RuntimeError("Non-finite SVG move coordinate")
                start = current
                last_was_close = False
            elif command == "L":
                if current is None or i + 1 >= len(tokens) or tokens[i] in ("M", "L", "A", "Z", "z") or tokens[i+1] in ("M", "L", "A", "Z", "z"):
                    raise RuntimeError("Incomplete SVG line command")
                end = (float(tokens[i]), float(tokens[i+1])); i += 2
                if not all(math.isfinite(v) for v in (*current, *end)):
                    raise RuntimeError("Non-finite SVG line coordinate")
                add_line(current, end)
                current = end
                last_was_close = False
            elif command == "A":
                if current is None or i + 6 >= len(tokens) or any(t in ("M", "L", "A", "Z", "z") for t in tokens[i:i+7]):
                    raise RuntimeError("Incomplete SVG arc command")
                args = [float(v) for v in tokens[i:i+7]]; i += 7
                rx, ry, rotation, large, sweep, ex, ey = args
                if (rx <= 0 or ry <= 0 or large not in (0.0, 1.0) or sweep not in (0.0, 1.0)
                        or current == (ex, ey) or not all(math.isfinite(v) for v in (*current, *args))):
                    raise RuntimeError("Invalid or degenerate SVG arc")
                end = (ex, ey)
                fwd_args = [rx, ry, rotation, int(large), int(sweep), ex, ey]
                reverse_args = [rx, ry, rotation, int(large), 1-int(sweep), current[0], current[1]]
                geometry = ("A", rx, ry, rotation, int(large), int(sweep))
                pieces.append({"start": current, "end": end,
                    "fwd": "M {:.12g} {:.12g} A {:.12g} {:.12g} {:.12g} {} {} {:.12g} {:.12g}".format(*current, *fwd_args),
                    "rev": "M {:.12g} {:.12g} A {:.12g} {:.12g} {:.12g} {} {} {:.12g} {:.12g}".format(*end, *reverse_args),
                    "geometry": geometry, "element": element})
                current = end
                last_was_close = False
            else:  # Z/z closes the current subpath with an implicit line if needed.
                if current is None or start is None:
                    raise RuntimeError("Close command without an active subpath")
                # SVG Z closes geometrically. If the serialized endpoint is
                # already within the join tolerance, do not manufacture a tiny
                # extra edge that creates a false degree-3/4 graph vertex.
                if not close(current, start):
                    add_line(current, start)
                current = start
                last_was_close = True
        if not pieces:
            raise RuntimeError("SVG path contains no usable segments")
        return pieces

    def segments(data, element):
        # Parse each M-delimited subpath independently, retaining every L/A edge.
        chunks = [p.strip() for p in re.split(r"(?=[Mm])", data) if p.strip()]
        result = []
        for chunk in chunks:
            result.extend(parse_fragment(chunk, element))
        return result

    # A shared ancestor transform is harmless: all geometry is expressed in the
    # same local coordinates (FreeCAD normally uses a common translated/flipped g).
    # Distinct transform chains must not be stitched or compared as local points.
    def transform_chain(node):
        chain = []
        while node is not None:
            if node.get("transform"):
                chain.append(node.get("transform"))
            node = parents.get(node)
        return tuple(reversed(chain))

    path_chains = {transform_chain(node) for node in root.iter()
                   if node.tag.rsplit("}", 1)[-1] in ("path", "circle")}
    if len(path_chains) > 1:
        raise RuntimeError("Different SVG transform contexts: cannot safely compare or join contours")
    scale_x, scale_y = _common_svg_scale(root)
    # Local-coordinate joining must never exceed the physical 0.001 mm limit.
    tolerance = ENDPOINT_TOLERANCE / max(scale_x, scale_y)

    # Keep style and parent context intact while allowing fragments from
    # separate path elements to join.
    grouped = {}
    for element in list(root.iter()):
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "circle":
            if not all(element.get(key) is not None for key in ("cx", "cy", "r")):
                raise RuntimeError("Invalid SVG circle: cx, cy and r are required")
            try:
                cx, cy, radius = (float(element.get(key))
                                  for key in ("cx", "cy", "r"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Invalid SVG circle coordinates or radius") from exc
            if not all(math.isfinite(v) for v in (cx, cy, radius)) or radius <= 0:
                raise RuntimeError("Invalid SVG circle: coordinates must be finite and radius must be positive")
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
        # Never join across distinct parent nodes: inherited styles, clip paths, and
        # coordinate context can differ even when the immediate attributes match.
        # Segments inside the same group still stitch normally.
        key = (id(parent), style)
        try:
            found = segments(element.get("d"), element)
        except Exception as exc:
            raise RuntimeError(translate("ShaperExport: Could not join path {}: {}").format(
                element.get("id", translate("(no ID)")), exc))
        grouped.setdefault(key, {"parent": parent, "style": style, "pieces": [], "elements": []})
        grouped[key]["pieces"].extend(found)
        grouped[key]["elements"].append(element)

    for group in grouped.values():
        pending = []
        def same_piece(a, b):
            # Never discard merely similar segments: only identical analytic
            # descriptions (possibly traversed backwards) are duplicates.
            ga, gb = a["geometry"], b["geometry"]
            if ga[0] != gb[0]:
                return False
            direct = a["start"] == b["start"] and a["end"] == b["end"]
            reverse = a["start"] == b["end"] and a["end"] == b["start"]
            if ga[0] == "L":
                return direct or reverse
            # Exact parameter match is sufficient, not necessary, for duplicate
            # geometry. Different valid arc encodings are retained; a graph
            # ambiguity then fails safely rather than silently losing geometry.
            same_shape = ga[1:5] == gb[1:5]
            return same_shape and ((direct and ga[5] == gb[5]) or
                                   (reverse and ga[5] != gb[5]))

        # Hash exact analytic segment representations instead of scanning every
        # previously accepted segment. This keeps duplicate removal near O(n).
        # Canonical endpoint order makes a reversed line/arc share the same key;
        # arc sweep is flipped when the segment direction is reversed.
        seen_piece_keys = {}
        def piece_key(piece):
            start, end = piece["start"], piece["end"]
            low, high = (start, end) if start <= end else (end, start)
            geometry = piece["geometry"]
            if geometry[0] == "L":
                return ("L", low, high)
            rx, ry, rotation, large, sweep = geometry[1:]
            canonical_sweep = sweep if start <= end else 1 - sweep
            return ("A", low, high, rx, ry, rotation, large, canonical_sweep)

        for piece in group["pieces"]:
            key = piece_key(piece)
            duplicate = seen_piece_keys.get(key)
            if duplicate is not None:
                first_id = duplicate['element'].get('id', '(no ID)')
                second_id = piece['element'].get('id', '(no ID)')
                App.Console.PrintWarning('ShaperExport: identical segment removed; source IDs: {} / {}\n'.format(first_id, second_id))
                continue
            seen_piece_keys[key] = piece
            pending.append(piece)
        # Build a tolerance-aware endpoint graph without recursive backtracking.
        # A vertex with degree > 2 is ambiguous: never guess a milling contour.
        # Collect every endpoint before clustering. A cluster must have a
        # diameter <= tolerance; ambiguous candidate membership is rejected.
        endpoints = [point for piece in pending for point in (piece["start"], piece["end"])]
        order = sorted(range(len(endpoints)), key=lambda k: (endpoints[k], k))
        clusters = []
        assignment = {}
        # Spatial hash: only inspect endpoint cells within one tolerance. This
        # avoids the previous all-clusters scan for large selections.
        cell_size = max(tolerance, 1e-12)
        grid = {}
        def cell_for(point):
            return (math.floor(point[0] / cell_size), math.floor(point[1] / cell_size))
        def nearby_endpoint_indices(point):
            gx, gy = cell_for(point)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for endpoint_index in grid.get((gx + dx, gy + dy), ()):
                        if close(point, endpoints[endpoint_index]):
                            yield endpoint_index
        for idx in order:
            point = endpoints[idx]
            nearby_indices = list(nearby_endpoint_indices(point))
            nearby_clusters = {assignment[j] for j in nearby_indices}
            candidates = [ci for ci in nearby_clusters
                          if all(close(point, endpoints[m]) for m in clusters[ci])]
            if len(candidates) > 1 or (nearby_clusters and len(candidates) != 1):
                raise RuntimeError(translate(
                    "Ambiguous endpoint clustering: tolerance relationships are inconsistent"))
            if candidates:
                ci = candidates[0]
                clusters[ci].append(idx)
            else:
                ci = len(clusters)
                clusters.append([idx])
            assignment[idx] = ci
            grid.setdefault(cell_for(point), []).append(idx)
        # Ensure two distinct clusters never contain points within tolerance.
        # Check only spatially neighbouring endpoints, not every cluster pair.
        for idx, point in enumerate(endpoints):
            for other_idx in nearby_endpoint_indices(point):
                if other_idx < idx and assignment[other_idx] != assignment[idx]:
                    raise RuntimeError(translate("Ambiguous endpoints across tolerance clusters"))
        endpoints_all = endpoints
        adjacency = [[] for _ in clusters]
        edges = []
        for index, piece in enumerate(pending):
            u, v = assignment[2 * index], assignment[2 * index + 1]
            edge_id = len(edges)
            edges.append((piece, u, v))
            adjacency[u].append(edge_id)
            adjacency[v].append(edge_id)
        if any(len(incident) > 2 for incident in adjacency):
            raise RuntimeError(translate("Ambiguous branching contour: more than two segments meet at an endpoint"))

        contours = []
        unused = set(range(len(edges)))
        # Walk edge IDs in stable order; repeatedly calling min(unused) would
        # add quadratic overhead when a drawing contains many disconnected cuts.
        for seed in range(len(edges)):
            if seed not in unused:
                continue
            component_edges = set()
            stack = [edges[seed][1]]
            visited_vertices = set()
            while stack:
                node = stack.pop()
                if node in visited_vertices:
                    continue
                visited_vertices.add(node)
                for eid in adjacency[node]:
                    component_edges.add(eid)
                    _, u, v = edges[eid]
                    stack.append(v if u == node else u)
            endpoints = [v for v in visited_vertices if len(adjacency[v]) == 1]
            if len(endpoints) not in (0, 2):
                raise RuntimeError(translate("Invalid contour topology: expected zero or two open endpoints"))
            closed = not endpoints
            node = min(endpoints) if endpoints else min(visited_vertices)
            cycle = []
            used = set()
            while True:
                candidates = [eid for eid in adjacency[node] if eid in component_edges and eid not in used]
                if not candidates:
                    break
                eid = min(candidates)
                used.add(eid)
                piece, u, v = edges[eid]
                reverse_piece = node == v and u != v
                cycle.append((piece, reverse_piece))
                node = u if reverse_piece else v
            if used != component_edges or (closed and node != (min(endpoints) if endpoints else min(visited_vertices))):
                raise RuntimeError(translate("Could not assemble an unambiguous contour"))
            unused.difference_update(used)
            # Serialize every shared endpoint using its graph vertex coordinate.
            # Never merely snap the final point: intermediate joints matter too.
            def vertex_coordinate(vid):
                members = clusters[vid]
                return endpoints_all[min(members, key=lambda k: (endpoints_all[k], k))]

            edge_lookup = {id(edges[eid][0]): eid for eid in used}
            first_piece, first_reverse = cycle[0]
            first_eid = edge_lookup[id(first_piece)]
            _, u, v = edges[first_eid]
            current_node = v if first_reverse else u
            start_node = current_node
            start = vertex_coordinate(start_node)
            commands = []
            for piece, reversed_piece in cycle:
                eid = edge_lookup[id(piece)]
                _, u, v = edges[eid]
                source_node = v if reversed_piece else u
                if source_node != current_node:
                    raise RuntimeError("Discontinuous graph traversal")
                target_node = u if reversed_piece else v
                target = vertex_coordinate(target_node)
                if math.dist(vertex_coordinate(current_node), target) <= BOUNDARY_TOLERANCE:
                    raise RuntimeError("Degenerate segment after endpoint snapping")
                if piece["geometry"][0] == "L":
                    commands.append("L {:.12g} {:.12g}".format(*target))
                else:
                    geom = piece["geometry"]
                    rx, ry, rotation, large, sweep = geom[1:]
                    if reversed_piece:
                        sweep = 1 - sweep
                    if math.dist(vertex_coordinate(current_node), target) <= BOUNDARY_TOLERANCE:
                        raise RuntimeError("Degenerate arc after endpoint snapping")
                    commands.append("A {:.12g} {:.12g} {:.12g} {} {} {:.12g} {:.12g}".format(
                        rx, ry, rotation, large, sweep, *target))
                current_node = target_node
            contour = "M {:.12g} {:.12g} ".format(*start) + " ".join(commands)
            if closed:
                if current_node != start_node:
                    raise RuntimeError("Closed graph traversal did not return to start")
                contour += " z"
            # Preserve provenance per connected component, not just from the
            # first SVG path in a same-style group. A stitched component made
            # from several source objects has intentionally ambiguous provenance.
            source_ids = sorted({
                piece["element"].get("data-source-id") or
                piece["element"].get("id") or "(no ID)"
                for piece, _reversed in cycle
            })
            source_id = source_ids[0] if len(source_ids) == 1 else "(joined: {})".format(", ".join(source_ids))
            contours.append((contour, source_id))

        parent = group["parent"]
        elements = group["elements"]
        local_elements = [el for el in elements if parents.get(el) is parent]
        insert_at = min((list(parent).index(el) for el in local_elements), default=len(list(parent)))
        template = elements[0]
        for el in elements:
            actual_parent = parents.get(el)
            if actual_parent is not None:
                actual_parent.remove(el)
        for offset, (contour, source_id) in enumerate(contours):
            separate = ET.Element(template.tag, dict(template.attrib))
            separate.set("d", contour)
            separate.attrib.pop("id", None)
            separate.set("data-source-id", source_id)
            parent.insert(insert_at + offset, separate)

    # Shaper Origin uses visible RGB geometry with real-world stroke units.
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] not in (
                "path", "circle", "line", "polyline", "polygon", "ellipse", "rect"):
            continue
        element.set("stroke", "#000000")
        element.set("fill", "#000000")
        element.set("stroke-width", "0.1")
        style = element.get("style")
        declarations = {"stroke": "#000000", "fill": "#000000",
                        "stroke-width": "0.1"}
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


def _sample_contour(data, arc_steps=16, max_sagitta=MAX_ARC_SAGITTA):
    """Flatten an absolute M/L/A contour for containment checks."""
    tokens = re.findall(r"[MLAZz]|[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?", data)
    if "".join(tokens) != re.sub(r"[\s,]+", "", data):
        raise ValueError("Unsupported or malformed SVG path command: {!r}".format(data))
    points = []
    current = start = None
    explicitly_closed = False
    i = 0
    try:
        while i < len(tokens):
            command = tokens[i]
            i += 1
            if explicitly_closed or (command == "M" and start is not None):
                raise ValueError("Unexpected command after close or repeated move")
            if command == "M":
                current = (float(tokens[i]), float(tokens[i + 1])); i += 2
                start = current
                points.append(current)
            elif command == "L":
                if current is None: raise ValueError("Line without move")
                current = (float(tokens[i]), float(tokens[i + 1])); i += 2
                points.append(current)
            elif command == "A":
                if current is None: raise ValueError("Arc without move")
                rx, ry, rotation = map(float, tokens[i:i + 3])
                if tokens[i + 3] not in ("0", "1") or tokens[i + 4] not in ("0", "1"):
                    raise ValueError("Invalid SVG arc flags")
                large, sweep = int(tokens[i + 3]), int(tokens[i + 4])
                end = (float(tokens[i + 5]), float(tokens[i + 6])); i += 7
                x1, y1 = current; x2, y2 = end
                phi = math.radians(rotation); cp, sp = math.cos(phi), math.sin(phi)
                rx, ry = abs(rx), abs(ry)
                if not all(math.isfinite(v) for v in (rx, ry, rotation, *end)) or rx <= 0 or ry <= 0 or current == end:
                    raise ValueError("Degenerate or non-finite SVG arc")
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
                    # Bound chord sagitta instead of assuming a fixed sample count.
                    max_radius = max(rx, ry)
                    if max_radius <= max_sagitta:
                        max_angle = math.pi / 8
                    else:
                        max_angle = 2 * math.acos(max(-1.0, min(1.0, 1 - max_sagitta / max_radius)))
                    count = max(1, int(math.ceil(abs(da) / max_angle)),
                                int(math.ceil(abs(da)/(2*math.pi)*arc_steps)))
                    if count > MAX_ARC_SEGMENTS:
                        raise ValueError("Arc requires more than {} samples for requested accuracy".format(MAX_ARC_SEGMENTS))
                    for step in range(1, count+1):
                        angle = a0+da*step/count
                        points.append((cx+cp*rx*math.cos(angle)-sp*ry*math.sin(angle),
                                       cy+sp*rx*math.cos(angle)+cp*ry*math.sin(angle)))
                points[-1] = end  # analytic endpoint, not a rounded trigonometric sample
                current = end
            elif command in ("Z", "z"):
                if current is None: raise ValueError("Close without move")
                # An explicit SVG close command defines a closed contour.
                # Compare sampled points, not the analytic arc endpoint.
                if math.dist(points[-1], start) <= BOUNDARY_TOLERANCE:
                    points[-1] = start
                else:
                    points.append(start)
                current = start
                explicitly_closed = True
            else:
                raise ValueError("Unsupported SVG path syntax")
    except (ValueError, IndexError, ZeroDivisionError, TypeError) as exc:
        raise ValueError("Invalid SVG contour {!r}: {}".format(data, exc)) from exc
    if explicitly_closed and (len(points) < 4 or points[0] != points[-1]):
        raise ValueError("Invalid closed SVG contour: {!r}".format(data))
    if not explicitly_closed and (len(points) < 2 or any(not all(math.isfinite(v) for v in p) for p in points)):
        raise ValueError("Invalid or empty open SVG path")
    return points if explicitly_closed else []


def _point_in_contour(point, polygon):
    x, y = point
    inside = False
    for a, b in zip(polygon, polygon[1:]):
        if (a[1] > y) != (b[1] > y):
            crossing = (b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]
            if x < crossing: inside = not inside
    return inside


def _orientation(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])


def _point_segment_distance(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0]-a[0])*dx + (p[1]-a[1])*dy)/length2))
    return math.hypot(p[0]-a[0]-t*dx, p[1]-a[1]-t*dy)


def _on_segment(p, a, b):
    return _point_segment_distance(p, a, b) <= BOUNDARY_TOLERANCE


def _segments_intersect(a, b, c, d):
    # Normalize orientation by segment length: the resulting quantity is mm.
    ab, cd = math.dist(a, b), math.dist(c, d)
    if ab <= BOUNDARY_TOLERANCE or cd <= BOUNDARY_TOLERANCE:
        return (_point_segment_distance(a, c, d) <= BOUNDARY_TOLERANCE or
                _point_segment_distance(c, a, b) <= BOUNDARY_TOLERANCE)
    o1, o2 = _orientation(a,b,c)/ab, _orientation(a,b,d)/ab
    o3, o4 = _orientation(c,d,a)/cd, _orientation(c,d,b)/cd
    t = BOUNDARY_TOLERANCE
    if (((o1 > t and o2 < -t) or (o1 < -t and o2 > t)) and
        ((o3 > t and o4 < -t) or (o3 < -t and o4 > t))):
        return True
    return (_on_segment(c,a,b) or _on_segment(d,a,b) or
            _on_segment(a,c,d) or _on_segment(b,c,d))


def _validate_closed_polygon(polygon, element_id):
    """Reject degenerate, self-intersecting and self-touching sampled contours."""
    if len(polygon) < 4 or polygon[0] != polygon[-1]:
        raise ValueError("Invalid closed contour {}".format(element_id))
    if any(not all(math.isfinite(v) for v in point) for point in polygon):
        raise ValueError("Non-finite contour coordinates: {}".format(element_id))
    edges = list(zip(polygon, polygon[1:]))
    if any(math.dist(a, b) <= BOUNDARY_TOLERANCE for a, b in edges):
        raise ValueError("Degenerate contour edge: {}".format(element_id))
    area2 = sum(a[0]*b[1] - b[0]*a[1] for a, b in edges)
    bounds = (min(p[0] for p in polygon), min(p[1] for p in polygon),
              max(p[0] for p in polygon), max(p[1] for p in polygon))
    if abs(area2) <= 2 * BOUNDARY_TOLERANCE * max(bounds[2]-bounds[0], bounds[3]-bounds[1]):
        raise ValueError("Zero-area or degenerate contour: {}".format(element_id))
    edge_boxes = [(min(a[0],b[0]), min(a[1],b[1]), max(a[0],b[0]), max(a[1],b[1])) for a,b in edges]
    order = sorted(range(len(edges)), key=lambda k: edge_boxes[k][0])
    for position, i in enumerate(order):
        a, b = edges[i]
        box_i = edge_boxes[i]
        for j in order[position + 1:]:
            box_j = edge_boxes[j]
            if box_j[0] > box_i[2] + BOUNDARY_TOLERANCE:
                break
            if box_j[1] > box_i[3] + BOUNDARY_TOLERANCE or box_j[3] < box_i[1] - BOUNDARY_TOLERANCE:
                continue
            adjacent = abs(i-j) == 1 or {i,j} == {0,len(edges)-1}
            if adjacent:
                c, d = edges[j]
                # Adjacent edges may meet at their shared vertex, but must not
                # retrace the same line segment beyond that single endpoint.
                shared = b if j == i + 1 else d if i == j + 1 else a
                other_a = a if b == shared else b
                other_b = c if d == shared else d
                va = (other_a[0] - shared[0], other_a[1] - shared[1])
                vb = (other_b[0] - shared[0], other_b[1] - shared[1])
                cross = va[0] * vb[1] - va[1] * vb[0]
                dot = va[0] * vb[0] + va[1] * vb[1]
                scale = max(math.hypot(*va), math.hypot(*vb), 1.0)
                if abs(cross) <= BOUNDARY_TOLERANCE * scale and dot > BOUNDARY_TOLERANCE ** 2:
                    raise ValueError("Adjacent contour edges overlap: {}".format(element_id))
                continue
            if _segments_intersect(a, b, *edges[j]):
                raise ValueError("Self-intersecting or self-touching contour: {}".format(element_id))


def _contour_relation(inner, outer):
    """Return 'inside', 'outside' or 'overlap' (including touching boundaries)."""
    outer_edges = list(zip(outer,outer[1:]))
    outer_index = sorted((min(c[0],d[0]), max(c[0],d[0]), min(c[1],d[1]), max(c[1],d[1]), c, d)
                         for c,d in outer_edges)
    for a,b in zip(inner,inner[1:]):
        xmin, xmax = min(a[0],b[0]), max(a[0],b[0])
        ymin, ymax = min(a[1],b[1]), max(a[1],b[1])
        for left, right, bottom, top, c, d in outer_index:
            if left > xmax + BOUNDARY_TOLERANCE:
                break
            if right < xmin - BOUNDARY_TOLERANCE or bottom > ymax + BOUNDARY_TOLERANCE or top < ymin - BOUNDARY_TOLERANCE:
                continue
            if _segments_intersect(a,b,c,d):
                return "overlap"
    return "inside" if _point_in_contour(inner[0], outer) else "outside"


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


def _analytic_path_bounds(data):
    """Exact local-coordinate bounds for absolute M/L/A/Z paths."""
    tokens = re.findall(r"[MLAZz]|[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?", data)
    if "".join(tokens) != re.sub(r"[\s,]+", "", data):
        raise ValueError("Unsupported path syntax while computing analytic bounds")
    points = []
    current = start = None
    i = 0
    while i < len(tokens):
        cmd = tokens[i]; i += 1
        if cmd == "M":
            current = (float(tokens[i]), float(tokens[i+1])); i += 2
            start = current; points.append(current)
        elif cmd == "L":
            if current is None: raise ValueError("Line without move in bounds calculation")
            current = (float(tokens[i]), float(tokens[i+1])); i += 2
            points.append(current)
        elif cmd == "A":
            if current is None: raise ValueError("Arc without move in bounds calculation")
            rx, ry, rotation = map(float, tokens[i:i+3])
            if tokens[i+3] not in ("0", "1") or tokens[i+4] not in ("0", "1"):
                raise ValueError("Invalid arc flags in bounds calculation")
            large, sweep = int(tokens[i+3]), int(tokens[i+4])
            end = (float(tokens[i+5]), float(tokens[i+6])); i += 7
            x1,y1=current; x2,y2=end
            if not all(math.isfinite(v) for v in (rx,ry,rotation,*current,*end)) or rx <= 0 or ry <= 0 or current == end:
                raise ValueError("Invalid arc in bounds calculation")
            rx, ry = abs(rx), abs(ry)
            phi=math.radians(rotation); cp,sp=math.cos(phi),math.sin(phi)
            dx,dy=(x1-x2)/2,(y1-y2)/2
            xp,yp=cp*dx+sp*dy,-sp*dx+cp*dy
            lam=xp*xp/(rx*rx)+yp*yp/(ry*ry)
            if lam > 1:
                factor=math.sqrt(lam); rx*=factor; ry*=factor
            numerator=max(0.0,rx*rx*ry*ry-rx*rx*yp*yp-ry*ry*xp*xp)
            denominator=rx*rx*yp*yp+ry*ry*xp*xp
            factor=(1 if large != sweep else -1)*math.sqrt(numerator/denominator) if denominator else 0.0
            cxp=factor*rx*yp/ry; cyp=factor*-ry*xp/rx
            cx=cp*cxp-sp*cyp+(x1+x2)/2
            cy=sp*cxp+cp*cyp+(y1+y2)/2
            theta0=math.atan2((yp-cyp)/ry,(xp-cxp)/rx)
            delta=math.atan2((-yp-cyp)/ry,(-xp-cxp)/rx)-theta0
            if sweep and delta < 0: delta += 2*math.pi
            if not sweep and delta > 0: delta -= 2*math.pi
            def on_arc(theta):
                if sweep:
                    return ((theta-theta0) % (2*math.pi)) <= delta + 1e-12 if delta >= 0 else False
                return ((theta0-theta) % (2*math.pi)) <= -delta + 1e-12 if delta <= 0 else False
            candidates=[theta0, theta0+delta]
            # Derivative-zero angles for x and y extrema of the rotated ellipse.
            tx=math.atan2(-sp*ry, cp*rx)
            ty=math.atan2(cp*ry, sp*rx)
            candidates.extend((tx,tx+math.pi,ty,ty+math.pi))
            for theta in candidates:
                if on_arc(theta):
                    points.append((cx+cp*rx*math.cos(theta)-sp*ry*math.sin(theta),
                                   cy+sp*rx*math.cos(theta)+cp*ry*math.sin(theta)))
            points.append(end); current=end
        elif cmd in ("Z","z"):
            if current is None or start is None: raise ValueError("Close without move in bounds calculation")
            current=start
        else:
            raise ValueError("Unsupported path command in bounds calculation")
    if not points or any(not math.isfinite(v) for pt in points for v in pt):
        raise ValueError("Cannot determine finite analytic path bounds")
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points))


def _classify_contours_by_size(svg_path, max_pocket_size):
    """Classify closed contours by size while keeping outer contours black."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    contours = []
    scale_x, scale_y = _common_svg_scale(root)
    # Arc approximation is specified in physical millimetres, but SVG paths are
    # sampled in local coordinates. Tighten local error when the SVG is enlarged.
    local_sagitta = MAX_ARC_SAGITTA / max(scale_x, scale_y)
    parents = {child: parent for parent in root.iter() for child in parent}
    for element in list(root.iter()):
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "path" and element.get("d"):
            # FreeCAD can consolidate multiple subpaths into one SVG path. Split
            # them so that every physical contour can receive its own size class.
            subpaths = [part.strip() for part in re.split(r"(?=[Mm])", element.get("d")) if part.strip()]
            evaluated = [(part, _sample_contour(part, arc_steps=CONTOUR_SAMPLE_STEPS,
                                                        max_sagitta=local_sagitta))
                         for part in subpaths]
            closed = [(part, polygon) for part, polygon in evaluated if polygon]
            if closed:
                parent = parents.get(element)
                insert_at = list(parent).index(element) if parent is not None else 0
                open_parts = [part for part, polygon in evaluated if not polygon]
                output_elements = []
                for part, polygon in closed:
                    contour = ET.Element(element.tag, dict(element.attrib))
                    contour.set("d", part)
                    # _force_closed_path_data removes per-fragment IDs and
                    # carries the original object ID in data-source-id. Prefer
                    # that provenance so classification does not erase it.
                    source_id = (element.get("data-source-id") or
                                 element.get("id") or "(no ID)")
                    _validate_closed_polygon(polygon, source_id)
                    contour.attrib.pop("id", None)
                    contour.set("data-source-id", source_id)
                    # Use analytic line/arc extrema for pocket classification; the
                    # sampled polygon is reserved for containment/intersection checks.
                    bounds = _analytic_path_bounds(part)
                    collision_bounds = (bounds[0] - local_sagitta, bounds[1] - local_sagitta,
                                        bounds[2] + local_sagitta, bounds[3] + local_sagitta)
                    contours.append((contour, bounds, collision_bounds, polygon))
                    output_elements.append(contour)
                if open_parts:
                    open_element = ET.Element(element.tag, dict(element.attrib))
                    open_element.set("d", " ".join(open_parts))
                    open_element.attrib.pop("id", None)
                    open_element.set("data-source-id", element.get("id", "(no ID)"))
                    output_elements.append(open_element)
                if parent is not None:
                    parent.remove(element)
                    for offset, output_element in enumerate(output_elements):
                        parent.insert(insert_at + offset, output_element)
            else:
                continue
        elif tag == "circle":
            if not all(element.get(key) is not None for key in ("cx", "cy", "r")):
                raise ValueError("Invalid SVG circle: cx, cy and r are required")
            try:
                cx, cy, radius = (float(element.get(key)) for key in ("cx", "cy", "r"))
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid SVG circle coordinates or radius") from exc
            if not all(math.isfinite(v) for v in (cx, cy, radius)) or radius <= 0:
                raise ValueError("Invalid SVG circle: coordinates must be finite and radius must be positive")
            if radius > 0:
                if radius > local_sagitta:
                    max_angle = 2 * math.acos(max(-1.0, min(1.0, 1 - local_sagitta / radius)))
                    count = max(64, int(math.ceil(2 * math.pi / max_angle)))
                else:
                    count = 64
                if count > MAX_ARC_SEGMENTS:
                    raise ValueError("Circle requires too many samples")
                polygon = [(cx+radius*math.cos(2*math.pi*i/count),
                            cy+radius*math.sin(2*math.pi*i/count)) for i in range(count)]
                polygon.append(polygon[0])
                _validate_closed_polygon(polygon, element.get("id", "(no ID)"))
                contours.append((element, (cx-radius, cy-radius, cx+radius, cy+radius),
                                 (cx-radius, cy-radius, cx+radius, cy+radius), polygon))
    # Test each contour pair only once. Disjoint bounding boxes cannot have
    # intersecting boundaries or contain one another, so skip costly edge tests.
    nested_flags = [False] * len(contours)
    def boxes_disjoint(a, b):
        return (a[2] < b[0] - BOUNDARY_TOLERANCE or
                b[2] < a[0] - BOUNDARY_TOLERANCE or
                a[3] < b[1] - BOUNDARY_TOLERANCE or
                b[3] < a[1] - BOUNDARY_TOLERANCE)

    for i, (element_a, measured_a, bounds_a, polygon_a) in enumerate(contours):
        for j in range(i + 1, len(contours)):
            element_b, measured_b, bounds_b, polygon_b = contours[j]
            if boxes_disjoint(bounds_a, bounds_b):
                continue
            relation_ab = _contour_relation(polygon_a, polygon_b)
            if relation_ab == "overlap":
                raise RuntimeError(translate(
                    "Intersecting or touching contours near SVG elements {} and {}: "
                    "cannot safely determine cut classification").format(
                        element_a.get("data-source-id", element_a.get("id", "(no ID)")), element_b.get("data-source-id", element_b.get("id", "(no ID)"))))
            if relation_ab == "inside":
                nested_flags[i] = True
            else:
                # Only test the reverse direction if A is not inside B.
                relation_ba = _contour_relation(polygon_b, polygon_a)
                if relation_ba == "overlap":
                    raise RuntimeError(translate(
                        "Intersecting or touching contours near SVG elements {} and {}: "
                        "cannot safely determine cut classification").format(
                            element_a.get("data-source-id", element_a.get("id", "(no ID)")), element_b.get("data-source-id", element_b.get("id", "(no ID)"))))
                if relation_ba == "inside":
                    nested_flags[j] = True

    for index, (element, bounds, collision_bounds, polygon) in enumerate(contours):
        width = (bounds[2] - bounds[0]) * scale_x
        height = (bounds[3] - bounds[1]) * scale_y
        is_pocket = width <= max_pocket_size and height <= max_pocket_size
        nested = nested_flags[index]
        if is_pocket:
            declarations = (("fill", "#777777"), ("stroke", "none"),
                            ("stroke-width", "0.1"))
        elif nested:
            declarations = (("fill", "#ffffff"), ("stroke", "#000000"), ("stroke-width", "0.1"))
        else:
            declarations = (("fill", "#000000"), ("stroke", "#000000"), ("stroke-width", "0.1"))
        _set_svg_style(element, dict(declarations + (
            ("opacity", "1"), ("fill-opacity", "1"), ("stroke-opacity", "1"))))
    tree.write(svg_path, encoding="utf-8", xml_declaration=True)


def _classify_open_paths(svg_path):
    """Encode open contours as gray on-line cuts, as Origin permits."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    declarations = {"fill": "none", "stroke": "#777777",
                    "stroke-width": "0.1", "opacity": "1",
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

    # Remember IDs before normalization, including source IDs carried through
    # contour splitting. The ID may already have been removed from the element.
    original_ids = {element: (element.get("id") or element.get("data-source-id", ""))
                    for element in elements}
    # Keep the first contour from each source object under its stable source ID.
    # If a source produced multiple contours, later ones receive fresh IDs.
    for element in elements:
        element_id = original_ids[element]
        if ((re.fullmatch(r"Shape2DView_nwe\d+", element_id)
             or re.fullmatch(r"circle\d+", element_id)) and element_id not in preserved):
            element.set("id", element_id)
            preserved.add(element_id)
        else:
            element.attrib.pop("id", None)

    next_number = {"path": 1, "circle": 1}
    for element in elements:
        if element.get("id"):
            continue
        prefix = ("circle" if element.get("data-shaper-circle") == "true"
                  or re.fullmatch(r"circle\d+", original_ids[element])
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
        element.attrib.pop("data-source-id", None)

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
        translate("Pocket size"),
        translate("Maximum width and height (mm):"),
        current, 0.001, 1000000.0, 2)
    if accepted:
        pref.SetFloat("max_pocket_size_mm", value)
        return value
    return None


def export_selection():
    selection=Gui.Selection.getSelection()
    if not selection:
        App.Console.PrintError("ShaperExport: %s\n" % translate("Please select a Shape2DView object first.")); return
    boxes=_selection_boxes(selection)
    if not boxes:
        App.Console.PrintError("ShaperExport: %s\n" % translate("No exportable shapes found.")); return
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
            Gui.getMainWindow(),translate("Save SVG file"),start_path,
            translate("SVG files (*.svg)"))
        filename=result[0] if isinstance(result,tuple) else result
    except Exception as exc:
        App.Console.PrintError("ShaperExport: %s\n" % (translate("Could not open file dialog: %s") % exc))
        return
    if not filename: return
    if not filename.lower().endswith(".svg"): filename+=".svg"
    temp_fd,temp_path=tempfile.mkstemp(suffix=".svg")
    os.close(temp_fd)
    publish_path = None
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
            raise RuntimeError(translate("FreeCAD's Shaper exporter did not create an SVG file"))
        _force_closed_path_data(temp_path)
        _classify_contours_by_size(temp_path, max_pocket_size)
        _classify_open_paths(temp_path)
        _normalize_shaper_opacity(temp_path)
        _normalize_contour_ids(temp_path)
        formatted_tree = ET.parse(temp_path)
        for node in formatted_tree.getroot().iter():
            if node.tag.rsplit("}", 1)[-1] == "path":
                presentation_properties = (
                    "alignment-baseline", "baseline-shift", "clip", "clip-path",
                    "clip-rule", "color", "color-interpolation",
                    "color-interpolation-filters", "color-profile", "color-rendering",
                    "cursor", "direction", "display", "dominant-baseline", "fill",
                    "fill-opacity", "fill-rule", "filter", "flood-color",
                    "flood-opacity", "font-family", "font-size", "font-stretch",
                    "font-style", "font-variant", "font-weight", "glyph-orientation-horizontal",
                    "glyph-orientation-vertical", "image-rendering", "letter-spacing",
                    "lighting-color", "marker-end", "marker-mid", "marker-start",
                    "mask", "opacity", "overflow", "paint-order", "pointer-events",
                    "shape-rendering", "stop-color", "stop-opacity", "stroke",
                    "stroke-dasharray", "stroke-dashoffset", "stroke-linecap",
                    "stroke-linejoin", "stroke-miterlimit", "stroke-opacity",
                    "stroke-width", "text-anchor", "text-decoration", "text-rendering",
                    "transform", "unicode-bidi", "vector-effect", "visibility",
                    "word-spacing", "writing-mode",
                )
                declarations = {}
                for name in presentation_properties:
                    if name in node.attrib:
                        declarations[name] = node.attrib.pop(name)
                for declaration in (node.get("style") or "").split(";"):
                    property_name, separator, value = declaration.partition(":")
                    property_name = property_name.strip().lower()
                    value = value.strip()
                    if separator and property_name and value:
                        declarations[property_name] = value
                if "fill-rule" in declarations:
                    declarations["fill-rule"] = re.sub(
                        r"^\s*evenodd\s*$", "evenodd", declarations["fill-rule"],
                        flags=re.IGNORECASE)
                if declarations:
                    node.set("style", ";".join(
                        "{}:{}".format(name, value)
                        for name, value in declarations.items()))
                else:
                    node.attrib.pop("style", None)
            if node.text is not None and not node.text.strip():
                node.text = None
            if node.tail is not None and not node.tail.strip():
                node.tail = None
        with open(temp_path, "wb") as target:
            target.write(_pretty_svg_xml(formatted_tree.getroot()).encode("utf-8"))
        # Validate the exact final bytes while they are still in the temporary
        # file. Never overwrite a previous good export before all checks pass.
        final_root = ET.parse(temp_path).getroot()
        path_count, closed_path_count, open_path_gaps = _path_statistics(final_root)
        final_ids = [node.get("id") for node in final_root.iter() if node.get("id")]
        if len(final_ids) != len(set(final_ids)):
            raise RuntimeError("Duplicate SVG element IDs remain after normalization")
        destination_dir = os.path.dirname(os.path.abspath(filename))
        publish_fd, publish_path = tempfile.mkstemp(prefix=".shaper-export-", suffix=".tmp", dir=destination_dir)
        with os.fdopen(publish_fd, "wb") as target, open(temp_path, "rb") as source:
            target.write(source.read())
            target.flush()
            os.fsync(target.fileno())
        os.replace(publish_path, filename)
        publish_path = None
    except Exception as exc:
        App.Console.PrintError("%s\n" % (translate("ShaperExport: Export failed: %s") % exc)); return
    finally:
        try: os.remove(temp_path)
        except OSError: pass
        if publish_path:
            try: os.remove(publish_path)
            except OSError: pass
    gap_list = ", ".join("{}: {:.4f} mm".format(path_id, gap)
                          for path_id, gap in open_path_gaps) or translate("No open paths")
    msg=translate("%d paths exported; %d closed shapes; %d open lines (start-to-end distances: %s).") % (
                path_count, closed_path_count, len(open_path_gaps), gap_list)
    App.Console.PrintMessage("ShaperExport: "+msg+"\n")
    try:
        try:
            from PySide import QtWidgets
        except ImportError:
            from PySide6 import QtWidgets
        rows = [
            (translate("Paths exported"), str(path_count)),
            (translate("Closed shapes"), str(closed_path_count)),
            (translate("Open lines"), str(len(open_path_gaps))),
        ]
        dialog = QtWidgets.QDialog(Gui.getMainWindow())
        dialog.setWindowTitle(translate("Export statistics"))
        layout = QtWidgets.QVBoxLayout(dialog)
        table = QtWidgets.QTableWidget(len(rows), 2, dialog)
        table.horizontalHeader().setVisible(False)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        for row_index, (label, value) in enumerate(rows):
            table.setItem(row_index, 0, QtWidgets.QTableWidgetItem(label))
            table.setItem(row_index, 1, QtWidgets.QTableWidgetItem(value))
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        table.setMinimumWidth(420)
        layout.addWidget(table)

        if open_path_gaps:
            gap_group = QtWidgets.QGroupBox(
                translate("Start-to-endpoint distances of open lines"), dialog)
            gap_layout = QtWidgets.QVBoxLayout(gap_group)
            gap_table = QtWidgets.QTableWidget(len(open_path_gaps), 2, gap_group)
            gap_table.setHorizontalHeaderLabels([translate("Path"),
                                                 translate("Distance")])
            gap_table.verticalHeader().setVisible(False)
            gap_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            gap_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
            for row_index, (path_id, gap) in enumerate(open_path_gaps):
                gap_table.setItem(row_index, 0, QtWidgets.QTableWidgetItem(path_id))
                gap_table.setItem(row_index, 1, QtWidgets.QTableWidgetItem(
                    "{:.4f} mm".format(gap)))
            gap_table.resizeColumnsToContents()
            gap_table.horizontalHeader().setStretchLastSection(True)
            gap_layout.addWidget(gap_table)
            layout.addWidget(gap_group)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok, parent=dialog)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        execute_dialog = getattr(dialog, "exec", None) or dialog.exec_
        execute_dialog()
    except Exception: pass
