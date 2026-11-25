from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from evans.domain.entities import (
    CadArc,
    CadBlock,
    CadBlockRef,
    CadCircle,
    CadDocumentInfo,
    CadEllipse,
    CadEntity,
    CadHatch,
    CadLayer,
    CadLine,
    CadMText,
    CadPoint,
    CadPolyline,
    CadRay,
    CadSolid,
    CadSpline,
    CadSystemInfo,
    CadText,
    CadXLine,
    CreateNurbCurveRequest,
    CreateNurbSurfaceRequest,
    EntityHandle,
    LayerName,
    ModifyNurbRequest,
    Point2D,
)
from evans.domain.exceptions import NotSupportedError
from evans.domain.interfaces import ICadRepository
from evans.infrastructure.http_bridge import HttpCadBridge, validate_file_path, validate_project_path

logger = logging.getLogger(__name__)


class CadRepository(ICadRepository):
    """CAD operations on the document bound to this native bridge."""

    def __init__(self, bridge: HttpCadBridge) -> None:
        self._http = bridge

    @property
    def connection_mode(self) -> str:
        return "full"

    def is_available(self) -> bool:
        return True

    def get_system_info(self) -> CadSystemInfo:
        info = self._http.get_system_info()
        if info:
            return CadSystemInfo(
                version=info.get("version", "unknown"),
                is_com_available=True,
                is_engine_available=True,
                active_documents=info.get("active_documents", 0),
            )
        raise RuntimeError("The native module did not return a result for get_system_info")

    # ── Internal helpers ───────────────────────────────────────

    def _ensure_entity_params(
        self, layer: LayerName | None, color: Any, linetype: Any, lineweight: Any
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if layer:
            params["layer"] = str(layer)
        if color is not None:
            params["color"] = color
        if linetype is not None:
            params["linetype"] = linetype
        if lineweight is not None:
            params["lineweight"] = lineweight
        return params

    def _to_handle(self, raw: str | None) -> EntityHandle | None:
        if raw:
            return EntityHandle(value=raw)
        return None

    # ── Entity Creation ────────────────────────────────────────

    def create_line(self, entity: CadLine) -> EntityHandle:
        params = {
            "x1": entity.start.x,
            "y1": entity.start.y,
            "x2": entity.end.x,
            "y2": entity.end.y,
            **self._ensure_entity_params(
                entity.layer, entity.color, entity.linetype, entity.lineweight
            ),
        }
        result = self._http.create_entity("line", params)
        if result:
            return EntityHandle(value=result)
        raise RuntimeError("The native module did not return a result for create_line")

    def create_circle(self, entity: CadCircle) -> EntityHandle:
        params = {
            "cx": entity.center.x,
            "cy": entity.center.y,
            "radius": entity.radius,
            **self._ensure_entity_params(
                entity.layer, entity.color, entity.linetype, entity.lineweight
            ),
        }
        result = self._http.create_entity("circle", params)
        if result:
            return EntityHandle(value=result)
        raise RuntimeError("The native module did not return a result for create_circle")

    def create_arc(self, entity: CadArc) -> EntityHandle:
        params = {
            "cx": entity.center.x,
            "cy": entity.center.y,
            "radius": entity.radius,
            "start_angle": entity.start_angle,
            "end_angle": entity.end_angle,
            **self._ensure_entity_params(
                entity.layer, entity.color, entity.linetype, entity.lineweight
            ),
        }
        result = self._http.create_entity("arc", params)
        if result:
            return EntityHandle(value=result)
        raise RuntimeError("The native module did not return a result for create_arc")

    def create_polyline(self, entity: CadPolyline) -> EntityHandle:
        params = {
            "vertices": [(v.x, v.y) for v in entity.vertices],
            "closed": entity.closed,
            **self._ensure_entity_params(
                entity.layer, entity.color, entity.linetype, entity.lineweight
            ),
        }
        result = self._http.create_entity("polyline", params)
        if result:
            return EntityHandle(value=result)
        raise RuntimeError("The native module did not return a result for create_polyline")

    def create_point(self, entity: CadPoint) -> EntityHandle:
        params = {
            "x": entity.position.x,
            "y": entity.position.y,
            **self._ensure_entity_params(
                entity.layer, entity.color, entity.linetype, entity.lineweight
            ),
        }
        result = self._http.create_entity("point", params)
        if result:
            return EntityHandle(value=result)
        raise RuntimeError("The native module did not return a result for create_point")

    def create_text(self, entity: CadText) -> EntityHandle:
        params = {
            "x": entity.insertion.x,
            "y": entity.insertion.y,
            "content": entity.content,
            "height": entity.height,
            "rotation": entity.rotation,
            **self._ensure_entity_params(
                entity.layer, entity.color, entity.linetype, entity.lineweight
            ),
        }
        result = self._http.create_entity("text", params)
        if result:
            return EntityHandle(value=result)
        raise RuntimeError("The native module did not return a result for create_text")

    def create_mtext(self, entity: CadMText) -> EntityHandle:
        params = {
            "top_left_x": entity.top_left.x,
            "top_left_y": entity.top_left.y,
            "bottom_right_x": entity.bottom_right.x,
            "bottom_right_y": entity.bottom_right.y,
            "content": entity.content,
            "height": entity.height,
            **self._ensure_entity_params(
                entity.layer, entity.color, entity.linetype, entity.lineweight
            ),
        }
        result = self._http.create_entity("mtext", params)
        h = self._to_handle(result)
        if h:
            return h
        msg = "Failed to create mtext"
        raise RuntimeError(msg)

    def create_ellipse(self, entity: CadEllipse) -> EntityHandle:
        params = {
            "cx": entity.center.x,
            "cy": entity.center.y,
            "major_axis_x": entity.major_axis_end.x,
            "major_axis_y": entity.major_axis_end.y,
            "radius_ratio": entity.radius_ratio,
            **self._ensure_entity_params(
                entity.layer, entity.color, entity.linetype, entity.lineweight
            ),
        }
        result = self._http.create_entity("ellipse", params)
        h = self._to_handle(result)
        if h:
            return h
        msg = "Failed to create ellipse"
        raise RuntimeError(msg)

    def create_spline(self, entity: CadSpline) -> EntityHandle:
        params = {
            "fit_points": [(p.x, p.y) for p in entity.fit_points],
            "degree": entity.degree,
            "closed": entity.closed,
            **self._ensure_entity_params(
                entity.layer, entity.color, entity.linetype, entity.lineweight
            ),
        }
        result = self._http.create_entity("spline", params)
        h = self._to_handle(result)
        if h:
            return h
        msg = "Failed to create spline"
        raise RuntimeError(msg)

    def create_ray(self, entity: CadRay) -> EntityHandle:
        params = {
            "p1_x": entity.start.x,
            "p1_y": entity.start.y,
            "p2_x": entity.start.x + entity.direction.x,
            "p2_y": entity.start.y + entity.direction.y,
            **self._ensure_entity_params(
                entity.layer, entity.color, entity.linetype, entity.lineweight
            ),
        }
        result = self._http.create_entity("ray", params)
        h = self._to_handle(result)
        if h:
            return h
        msg = "Failed to create ray"
        raise RuntimeError(msg)

    def create_xline(self, entity: CadXLine) -> EntityHandle:
        params = {
            "p1_x": entity.through.x,
            "p1_y": entity.through.y,
            "p2_x": entity.through.x + entity.direction.x,
            "p2_y": entity.through.y + entity.direction.y,
            **self._ensure_entity_params(
                entity.layer, entity.color, entity.linetype, entity.lineweight
            ),
        }
        result = self._http.create_entity("xline", params)
        h = self._to_handle(result)
        if h:
            return h
        msg = "Failed to create xline"
        raise RuntimeError(msg)

    def create_solid(self, entity: CadSolid) -> EntityHandle:
        params = {
            "points": [(p.x, p.y) for p in entity.points],
            **self._ensure_entity_params(
                entity.layer, entity.color, entity.linetype, entity.lineweight
            ),
        }
        result = self._http.create_entity("solid", params)
        h = self._to_handle(result)
        if h:
            return h
        msg = "Failed to create solid"
        raise RuntimeError(msg)

    def create_hatch(self, entity: CadHatch) -> EntityHandle:
        params = {
            "pattern_name": entity.pattern_name,
            "pattern_scale": entity.pattern_scale,
            "pattern_angle": entity.pattern_angle,
            "associative": entity.associative,
            **self._ensure_entity_params(
                entity.layer, entity.color, entity.linetype, entity.lineweight
            ),
        }
        result = self._http.create_entity("hatch", params)
        h = self._to_handle(result)
        if h:
            return h
        msg = "Failed to create hatch"
        raise RuntimeError(msg)

    # ── Entity Manipulation ────────────────────────────────────

    def get_entity(self, handle: EntityHandle) -> CadEntity | None:
        data = self._http.get_entity(str(handle))
        if data:
            return CadEntity.model_validate(data)
        return None

    def delete_entity(self, handle: EntityHandle) -> bool:
        return self._http.delete_entity(str(handle))

    def move_entity(self, handle: EntityHandle, dx: float, dy: float) -> bool:
        if self._http.move_entity(str(handle), dx, dy):
            return True
        msg = "Move via COM not implemented"
        raise NotSupportedError(msg)

    def copy_entity(self, handle: EntityHandle) -> EntityHandle | None:
        result = self._http.copy_entity(str(handle))
        return self._to_handle(result)
        msg = "Copy via COM not implemented"
        raise NotSupportedError(msg)

    def rotate_entity(
        self, handle: EntityHandle, angle: float, center: Point2D | None = None
    ) -> bool:
        cx = center.x if center else None
        cy = center.y if center else None
        if self._http.rotate_entity(str(handle), angle, cx, cy):
            return True
        msg = "Rotate via COM not implemented"
        raise NotSupportedError(msg)

    def scale_entity(
        self, handle: EntityHandle, factor: float, center: Point2D | None = None
    ) -> bool:
        cx = center.x if center else None
        cy = center.y if center else None
        if self._http.scale_entity(str(handle), factor, cx, cy):
            return True
        msg = "Scale via COM not implemented"
        raise NotSupportedError(msg)

    def mirror_entity(self, handle: EntityHandle, p1: Point2D, p2: Point2D) -> bool:
        result = self._http._request(
            "POST",
            f"/api/entity/{handle}/mirror",
            json_body={"p1_x": p1.x, "p1_y": p1.y, "p2_x": p2.x, "p2_y": p2.y},
        )
        if result is None:
            return False
        return result.get("success", True)

    def set_entity_layer(self, handle: EntityHandle, layer: LayerName) -> bool:
        result = self._http._request(
            "PATCH",
            f"/api/entity/{handle}/layer",
            json_body={"layer": str(layer)},
        )
        if result is None:
            return False
        return result.get("success", True)
        msg = "Set layer via COM not implemented"
        raise NotSupportedError(msg)

    def get_entities_by_type(
        self, entity_type: str, layer: LayerName | None = None
    ) -> list[CadEntity]:
        params: dict[str, Any] = {"entity_type": entity_type, "max_count": 1000}
        if layer:
            params["layer"] = str(layer)
        if self._http:
            result = self._http._request("POST", "/api/selection/select", json_body=params)
            if result and "entities" in result:
                return [CadEntity.model_validate(e) for e in result["entities"]]
        return []

    # ── Layer Management ───────────────────────────────────────

    def create_layer(self, layer: CadLayer) -> None:
        if self._http.create_layer(str(layer.name), str(layer.color)):
            return
        raise RuntimeError("The native module did not return a result for create_layer")

    def get_linetypes(self) -> list[dict[str, Any]]:
        """Get all linetypes in the drawing."""
        linetypes_data = self._http.get_linetypes()
        if linetypes_data:
            return linetypes_data
        # COM fallback — not available via COM, return empty list
        return []

    def get_layers(self) -> list[CadLayer]:
        layers_data = self._http.get_layers()
        if layers_data:
            return [
                CadLayer(
                    name=LayerName(value=l["name"]),
                    is_on=l.get("is_on", True),
                    is_frozen=l.get("is_frozen", False),
                    is_locked=l.get("is_locked", False),
                )
                for l in layers_data
            ]
        raise RuntimeError("The native module did not return a result for get_layers")

    def set_current_layer(self, name: LayerName) -> None:
        if not self._http.set_current_layer(str(name)):
            raise RuntimeError("The native module could not complete set_current_layer")

    def delete_layer(self, name: LayerName) -> bool:
        result = self._http._request("DELETE", f"/api/layer/{name}")
        if result is None:
            return False
        return result.get("success", True)
        msg = "Delete layer via COM not implemented"
        raise NotSupportedError(msg)

    def set_layer_state(
        self,
        name: LayerName,
        on: bool | None = None,
        frozen: bool | None = None,
        locked: bool | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if on is not None:
            body["on"] = on
        if frozen is not None:
            body["frozen"] = frozen
        if locked is not None:
            body["locked"] = locked
        self._http._request("PATCH", f"/api/layer/{name}", json_body=body)

    # ── Block Operations ───────────────────────────────────────

    def create_block(self, block: CadBlock) -> None:
        self._http._request("POST", "/api/block/create", json_body=block.model_dump())

    def insert_block(self, block_ref: CadBlockRef) -> EntityHandle:
        handle = self._http.insert_block(
            name=str(block_ref.block_name),
            x=block_ref.insertion.x,
            y=block_ref.insertion.y,
            scale=block_ref.scale_x,
            rotation=block_ref.rotation,
        )
        if handle:
            return EntityHandle(value=handle)
        msg = "Failed to insert block"
        raise RuntimeError(msg)

    def get_blocks(self) -> list[CadBlock]:
        result = self._http._request("GET", "/api/block")
        if result and "blocks" in result:
            return [CadBlock.model_validate(b) for b in result["blocks"]]
        return []

    def delete_block(self, name: LayerName) -> bool:
        result = self._http._request("DELETE", f"/api/block/{name}")
        if result is None:
            return False
        return result.get("success", True)

    def get_block_entities(self, name: str) -> list[dict[str, Any]]:
        result = self._http._request("GET", f"/api/block/{name}/entities")
        if result and "entities" in result:
            return result["entities"]
        return []

    # ── Document Operations ────────────────────────────────────

    def get_document_info(self) -> CadDocumentInfo:
        info = self._http.get_document_info()
        if info:
            return CadDocumentInfo(
                name=info.get("name", ""),
                path=info.get("path", ""),
                is_saved=info.get("is_saved", False),
                entities_count=info.get("entities_count", 0),
                layers_count=info.get("layers_count", 0),
                blocks_count=info.get("blocks_count", 0),
            )
        raise RuntimeError("The native module did not return a result for get_document_info")

    def save_document(self, path: str | None = None) -> None:
        if not self._http.save_document(path):
            raise RuntimeError("The native module could not complete save_document")

    def export_pdf(self, path: str) -> None:
        if not self._http.export_pdf(path):
            raise RuntimeError("The native module could not complete export_pdf")

    def export_dwg(self, path: str) -> None:
        safe = validate_file_path(path)
        self._http._request("POST", "/api/document/export/dwg", json_body={"path": safe})

    def export_dxf(self, path: str) -> None:
        safe = validate_file_path(path)
        self._http._request("POST", "/api/document/export/dxf", json_body={"path": safe})

    def screenshot(self, path: str, width: int = 1920, height: int = 1080) -> bool:
        return self._http.screenshot(path=path, width=width, height=height)

    def zoom_extents(self) -> None:
        self._http._request("POST", "/api/document/zoom/extents")
        return

    def new_document(self, template: str | None = None) -> None:
        body = {"template": template} if template else {}
        self._http._request("POST", "/api/document/new", json_body=body)

    def create_project(
        self,
        filename: str,
        directory: str,
        template: str | None = None,
    ) -> None:
        """Create a new project file at ``directory/filename``.

        Validates path against traversal. Creates directory if missing.
        Raises ValidationError or RuntimeError on failure.
        """
        if not filename:
            raise ValueError("filename is required")
        if not directory:
            raise ValueError("directory is required")
        validated = validate_project_path(directory, filename)
        Path(validated).parent.mkdir(parents=True, exist_ok=True)
        ok = self._http.create_project(filename=filename, directory=directory, template=template)
        if not ok:
            raise RuntimeError(f"create_project failed: {directory}/{filename}")

    def save_project(self, filename: str, directory: str) -> None:
        """Save current document to ``directory/filename`` (overwrites).

        Validates path against traversal. Creates directory if missing.
        """
        if not filename:
            raise ValueError("filename is required")
        if not directory:
            raise ValueError("directory is required")
        validated = validate_project_path(directory, filename)
        Path(validated).parent.mkdir(parents=True, exist_ok=True)
        ok = self._http.save_project(filename=filename, directory=directory)
        if not ok:
            raise RuntimeError(f"save_project failed: {directory}/{filename}")

    def open_document(self, path: str) -> None:
        safe = validate_file_path(path)
        self._http._request("POST", "/api/document/open", json_body={"path": safe})

    def close_document(self) -> None:
        self._http._request("POST", "/api/document/close")

    # ── System ─────────────────────────────────────────────────

    def get_system_fonts(self) -> list[dict[str, Any]]:
        return self._http.get_system_fonts()
        return []

    def execute_command(self, command: str) -> str | None:
        return self._http.execute_command(command)
        msg = "Command execution requires .NET engine"
        raise NotSupportedError(msg)

    def get_system_variable(self, name: str) -> str | None:
        return self._http.get_system_variable(name)

    def set_system_variable(self, name: str, value: str) -> None:
        self._http.set_system_variable(name, value)
        return

    # ── Extended HTTP-only operations ──────────────────────

    def create_helix(self, **kwargs: Any) -> Any:
        return self._http.create_helix(**kwargs) if self._http else None

    def create_region(self, **kwargs: Any) -> Any:
        return self._http.create_region(**kwargs) if self._http else None

    def create_boundary(self, **kwargs: Any) -> Any:
        return self._http.create_boundary(**kwargs) if self._http else None

    def create_gradient(self, **kwargs: Any) -> Any:
        return self._http.create_gradient(**kwargs) if self._http else None

    def create_arc_length_dimension(self, **kwargs: Any) -> Any:
        return self._http.create_arc_length_dimension(**kwargs) if self._http else None

    def export_ifc(self, **kwargs: Any) -> Any:
        return self._http.export_ifc(**kwargs) if self._http else None

    def create_mesh(self, **kwargs: Any) -> Any:
        return self._http.create_mesh(**kwargs) if self._http else None

    def edit_mesh(self, **kwargs: Any) -> Any:
        return self._http.edit_mesh(**kwargs) if self._http else None

    def set_viewport(self, **kwargs: Any) -> Any:
        return self._http.set_viewport(**kwargs) if self._http else None

    def render(self, **kwargs: Any) -> Any:
        return self._http.render(**kwargs) if self._http else None

    # ── 3D Solids ─────────────────────────────────────────────

    def create_box(self, x: float, y: float, z: float) -> str | None:
        return self._http.create_box(x, y, z)

    def create_sphere(self, radius: float) -> str | None:
        return self._http.create_sphere(radius)

    def create_cylinder(self, radius: float, height: float) -> str | None:
        return self._http.create_cylinder(radius, height)

    def create_cone(self, radius_bottom: float, height: float) -> str | None:
        return self._http.create_cone(radius_bottom, height)

    def create_torus(self, major_radius: float, minor_radius: float) -> str | None:
        return self._http.create_torus(major_radius, minor_radius)

    def create_wedge(self, x: float, y: float, z: float) -> str | None:
        return self._http.create_wedge(x, y, z)

    def create_pyramid(self, height: float, sides: int, radius: float) -> str | None:
        return self._http.create_pyramid(height, sides, radius)

    def boolean_union(self, h1: str, h2: str) -> str | None:
        return self._http.boolean_union(h1, h2)

    def boolean_subtract(self, h1: str, h2: str) -> str | None:
        return self._http.boolean_subtract(h1, h2)

    def boolean_intersect(self, h1: str, h2: str) -> str | None:
        return self._http.boolean_intersect(h1, h2)

    def extrude_solid(self, handle: str, height: float, taper_angle: float = 0) -> str | None:
        return self._http.extrude_solid(handle, height, taper_angle)

    def revolve_solid(
        self,
        handle: str,
        axis_x: float = 0,
        axis_y: float = 0,
        axis_z: float = 0,
        dir_x: float = 0,
        dir_y: float = 0,
        dir_z: float = 1,
        angle: float = 360,
    ) -> str | None:
        return self._http.revolve_solid(handle, axis_x, axis_y, axis_z, dir_x, dir_y, dir_z, angle)

    def rotate_solid(
        self,
        handle: str,
        angle: float,
        cx: float = 0,
        cy: float = 0,
        cz: float = 0,
        ax: float = 0,
        ay: float = 0,
        az: float = 1,
    ) -> bool:
        return self._http.rotate_solid(handle, angle, cx, cy, cz, ax, ay, az)

    def move_solid(self, handle: str, dx: float, dy: float, dz: float = 0) -> bool:
        return self._http.move_solid(handle, dx, dy, dz)

    def set_3d_view(self, direction: str, render_mode: str = "wireframe") -> bool:
        return self._http.set_3d_view(direction, render_mode)

    def get_solid_properties(self, handle: str) -> dict[str, Any] | None:
        return self._http.get_solid_properties(handle)

    # ── NURBS / IFC ────────────────────────────────────────

    def create_nurb_curve(self, request: CreateNurbCurveRequest) -> EntityHandle | None:
        result = self._http.create_nurb_curve(**request.model_dump())
        handle = result.get("handle") if result else None
        return EntityHandle(value=handle) if handle else None

    def create_nurb_surface(self, request: CreateNurbSurfaceRequest) -> EntityHandle | None:
        result = self._http.create_nurb_surface(**request.model_dump())
        handle = result.get("handle") if result else None
        return EntityHandle(value=handle) if handle else None

    def modify_nurb(self, request: ModifyNurbRequest) -> bool:
        result = self._http.modify_nurb(**request.model_dump())
        if result is None:
            return False
        return result.get("success", True)

    def import_ifc(self, path: str) -> bool:
        return self._http.import_ifc(path=path)

    def get_ifc_entities(self) -> list[dict[str, Any]] | None:
        return self._http.get_ifc_entities()

    # ── MultiCAD API ────────────────────────────────────────

    def create_grid_axis(self, request: Any) -> bool:
        return self._http.create_grid_axis(
            grid_type=request.type,
            origin_x=request.origin_x,
            origin_y=request.origin_y,
            spacings_x=request.spacings_x,
            spacings_y=request.spacings_y,
            naming_x=request.naming_x,
            naming_y=request.naming_y,
        )
        return False

    def create_grid_label(self, request: Any) -> bool:
        return self._http.create_grid_label(
            grid_handle=request.grid_handle,
            label=request.label,
            axis_index=request.axis_index,
            direction=request.direction,
        )
        return False

    def create_room(self, request: Any) -> bool:
        return self._http.create_room(
            x=request.x,
            y=request.y,
            width=request.width,
            height=request.height,
            name=request.name,
        )
        return False

    def get_room_properties(self, handle: str) -> dict[str, Any] | None:
        return self._http.get_room_properties(handle=handle)
        return None

    def create_custom_object(self, request: Any) -> bool:
        return self._http.create_custom_object(
            class_name=request.class_name,
            properties=request.properties,
        )
        return False

    def create_parametric_object(self, request: Any) -> bool:
        return self._http.create_parametric_object(
            object_type=request.type,
            parameters=request.parameters,
        )
        return False

    def create_reactor(self, request: Any) -> bool:
        return self._http.create_reactor(
            entity_handle=request.entity_handle,
            event_type=request.event_type,
        )
        return False

    def create_2d_break(self, request: Any) -> bool:
        return self._http.create_2d_break(
            view_handle=request.view_handle,
            x1=request.x1,
            y1=request.y1,
            x2=request.x2,
            y2=request.y2,
        )
        return False

    def start_motion_preview(self, request: Any) -> bool:
        return self._http.start_motion_preview(handle=request.handle)
        return False

    def stop_motion_preview(self) -> bool:
        return self._http.stop_motion_preview()
        return False

    def create_body_contour(self, request: Any) -> bool:
        return self._http.create_body_contour(solid_handle=request.solid_handle)
        return False

    def check_3d_faces(self, handle: str) -> dict[str, Any] | None:
        return self._http.check_3d_faces(handle=handle)
        return None
