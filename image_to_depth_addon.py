bl_info = {
    "name": "Image to Depth",
    "blender": (4, 0, 0),
    "category": "Object",
    "author": "Bhargav Sai Chishti",
    "description": "Pick an image, call external run.py (Depth Anything V2) via your venv Python, then apply the depth map as displacement on a plane.",
}

import bpy
import subprocess
from pathlib import Path

# --------- Properties stored on the Scene ----------
class IDepthProps(bpy.types.PropertyGroup):
    filepath: bpy.props.StringProperty(
        name="Input Image",
        description="Choose Image to extract 3d Map",
        subtype="FILE_PATH",
        default=""
    )
    recent_images: bpy.props.CollectionProperty(
        type=bpy.types.PropertyGroup,
        name="Recent Images"
    )
    show_recent: bpy.props.BoolProperty(
        name="Show Recent Images",
        default=False
    )
    progress: bpy.props.FloatProperty(
        name="Progress",
        default=0.0,
        min=0.0,
        max=100.0,
        subtype='PERCENTAGE'
    )
    is_processing: bpy.props.BoolProperty(
        name="Processing",
        default=False
    )
    outdir: bpy.props.StringProperty(
        name="Output Folder",
        description="Folder where depth map PNG will be saved",
        subtype="DIR_PATH",
        default=""
    )
    venv_python: bpy.props.StringProperty(
        name="Venv python.exe",
        description="Full path to the Python interpreter that has torch/timm installed",
        subtype="FILE_PATH",
        default=r"C:\Users\LENOVO\Dsa\DepthMapBlender\.venv\Scripts\python.exe"
    )
    repo_dir: bpy.props.StringProperty(
        name="Depth-Anything-V2 Folder",
        description="Folder that contains run.py, checkpoints/, assets/, etc.",
        subtype="DIR_PATH",
        default=r"C:\Users\LENOVO\Dsa\DepthMapBlender\Depth-Anything-V2"
    )
    encoder: bpy.props.EnumProperty(
        name="Encoder",
        items=[('vits',"vits",""),('vitb',"vitb",""),('vitl',"vitl",""),('vitg',"vitg","")],
        default='vitb'
    )
    input_size: bpy.props.IntProperty(
        name="Input Size",
        description="Depth Anything V2 input size",
        default=518, min=64, max=2048
    )
    grayscale: bpy.props.BoolProperty(
        name="Grayscale",
        description="Save non-colored depth (script --grayscale)",
        default=True
    )
    pred_only: bpy.props.BoolProperty(
        name="Pred-only",
        description="Save depth only (no side-by-side) (script --pred-only)",
        default=True
    )
    subdiv_cuts: bpy.props.IntProperty(
        name="Subdivide cuts",
        description="Geometry density for displacement",
        default=200, min=20, max=1000
    )
    disp_scale: bpy.props.FloatProperty(
        name="Displacement scale",
        description="Displacement node scale factor",
        default=0.5, min=0.0, soft_max=5.0
    )

# ---- helpers -------------------------------------------------
def add_to_recent_images(filepath):
    """Add image to recent images list, avoiding duplicates"""
    scene = bpy.context.scene
    recent = scene.idepth.recent_images
    
    # Remove if already exists
    for i, item in enumerate(recent):
        if item.name == filepath:
            recent.remove(i)
            break
    
    # Add to beginning
    item = recent.add()
    item.name = filepath
    
    # Keep only last 5 items
    while len(recent) > 5:
        recent.remove(len(recent) - 1)

def update_progress(percentage, message=""):
    """Update progress bar and UI"""
    bpy.context.scene.idepth.progress = percentage
    if message:
        print(f"Progress: {percentage}% - {message}")
    
    # Force UI update - this is the key part!
    bpy.context.view_layer.update()
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()
    
    # Process events to update UI immediately
    bpy.app.handlers.depsgraph_update_post.append(lambda scene, depsgraph: None)
    bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)

class RecentImageItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
def force_material_tab_if_possible():
    # Try to switch any Properties editor to MATERIAL tab (optional UX nicety)
    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == 'PROPERTIES':
                    for space in area.spaces:
                        if space.type == 'PROPERTIES':
                            space.context = 'MATERIAL'
        return True
    except Exception:
        return False

def set_displacement_only(obj, mat):
    """Robustly set the material to true displacement, regardless of UI context."""
    # Render engine must be Cycles
    bpy.context.scene.render.engine = 'CYCLES'

    # Make sure the material is active on the object
    if obj.active_material is not mat:
        obj.active_material = mat

    # Try both properties (Blender versions differ)
    try:
        mat.displacement_method = 'DISPLACEMENT'
    except Exception:
        pass
    try:
        mat.cycles.displacement_method = 'DISPLACEMENT'
    except Exception:
        pass

    # Optional: put Properties editor on Material tab
    force_material_tab_if_possible()

    # Ensure depsgraph updates
    bpy.context.view_layer.update()

# --------- Operators for recent images ----------
class IDepth_OT_SelectRecent(bpy.types.Operator):
    bl_idname = "idepth.select_recent"
    bl_label = "Select Recent Image"
    bl_description = "Select a recently used image"
    
    filepath: bpy.props.StringProperty()
    
    def execute(self, context):
        context.scene.idepth.filepath = self.filepath
        return {'FINISHED'}

class IDepth_OT_ToggleRecent(bpy.types.Operator):
    bl_idname = "idepth.toggle_recent"
    bl_label = "Toggle Recent Images"
    bl_description = "Show/hide recent images list"
    
    def execute(self, context):
        context.scene.idepth.show_recent = not context.scene.idepth.show_recent
        return {'FINISHED'}

# --------- Drag and Drop Handler ----------
class IDepth_OT_DropImage(bpy.types.Operator):
    bl_idname = "idepth.drop_image"
    bl_label = "Drop Image"
    bl_description = "Handle dropped image files"
    
    def modal(self, context, event):
        return {'PASS_THROUGH'}
    
    def invoke(self, context, event):
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}
class IDepth_OT_Run(bpy.types.Operator):
    bl_idname = "idepth.run"
    bl_label = "Run Depth Mapping"
    bl_description = "Runs Depth-Anything-V2 run.py using your venv, then builds a displaced plane"

    def execute(self, context):
        p = context.scene.idepth
        
        # Set processing state
        p.is_processing = True
        p.progress = 0.0
        update_progress(0, "Starting validation...")

        # --- Validate inputs ---
        if not p.filepath or not Path(p.filepath).is_file():
            self.report({'ERROR'}, "Please set a valid Input Image.")
            p.is_processing = False
            return {'CANCELLED'}
        if not p.outdir:
            self.report({'ERROR'}, "Please set an Output Folder.")
            p.is_processing = False
            return {'CANCELLED'}
        if not p.venv_python or not Path(p.venv_python).is_file():
            self.report({'ERROR'}, "Venv python.exe path is invalid.")
            p.is_processing = False
            return {'CANCELLED'}
        if not p.repo_dir or not Path(p.repo_dir).is_dir():
            self.report({'ERROR'}, "Depth-Anything-V2 folder is invalid.")
            p.is_processing = False
            return {'CANCELLED'}

        update_progress(10, "Validation complete, preparing paths...")

        img_path = str(Path(p.filepath))
        outdir   = str(Path(p.outdir))
        repo_dir = str(Path(p.repo_dir))
        run_py   = str(Path(repo_dir) / "run.py")

        Path(outdir).mkdir(parents=True, exist_ok=True)
        
        # Add to recent images
        add_to_recent_images(img_path)

        cmd = [
            p.venv_python,
            run_py,
            "--encoder", p.encoder,
            "--img-path", img_path,
            "--input-size", str(p.input_size),
            "--outdir", outdir,
        ]
        if p.grayscale:
            cmd.append("--grayscale")
        if p.pred_only:
            cmd.append("--pred-only")

        update_progress(20, "Running depth estimation...")

        # --- Run external process with the repo as CWD so relative paths resolve ---
        try:
            self.report({'INFO'}, "Running depth script… (check System Console for logs)")
            
            # Start the process
            process = subprocess.Popen(
                cmd,
                cwd=repo_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Monitor progress while process runs
            progress_step = 20
            max_progress = 60
            while process.poll() is None:
                if progress_step < max_progress:
                    progress_step += 5
                    update_progress(progress_step, "Processing depth estimation...")
                    # Small delay to prevent UI freezing
                    import time
                    time.sleep(0.5)
            
            # Get final result
            stdout, stderr = process.communicate()
            
            if process.returncode != 0:
                print("=== DepthAnythingV2 STDOUT (error) ===\n", stdout)
                print("=== DepthAnythingV2 STDERR (error) ===\n", stderr)
                self.report({'ERROR'}, f"Script failed (see console). Exit code {process.returncode}")
                p.is_processing = False
                return {'CANCELLED'}
            
            print("=== DepthAnythingV2 STDOUT ===\n", stdout)
            print("=== DepthAnythingV2 STDERR ===\n", stderr)
            update_progress(60, "Depth estimation complete, loading images...")
            
        except Exception as e:
            self.report({'ERROR'}, f"Failed to launch script: {e}")
            p.is_processing = False
            return {'CANCELLED'}

        # Expected output file name
        depth_png = str(Path(outdir) / (Path(img_path).stem + ".png"))
        if not Path(depth_png).is_file():
            self.report({'ERROR'}, f"Depth map not found: {depth_png}")
            p.is_processing = False
            return {'CANCELLED'}

        update_progress(70, "Creating 3D mesh...")

        # --- Import images and build displaced plane ---
        bpy.context.scene.render.engine = 'CYCLES'

        # Load depth map image
        try:
            depth_image = bpy.data.images.load(depth_png)
        except RuntimeError:
            depth_image = bpy.data.images.get(Path(depth_png).name)

        # Load original image for texture
        try:
            original_image = bpy.data.images.load(img_path)
        except RuntimeError:
            original_image = bpy.data.images.get(Path(img_path).name)

        update_progress(75, "Calculating mesh dimensions...")

        # Calculate aspect ratio from original image
        img_width = original_image.size[0]
        img_height = original_image.size[1]
        aspect_ratio = img_width / img_height
        
        # Create plane with matching aspect ratio
        if aspect_ratio >= 1.0:
            # Landscape or square - width is larger or equal
            plane_scale_x = 2.0
            plane_scale_y = 2.0 / aspect_ratio
        else:
            # Portrait - height is larger
            plane_scale_x = 2.0 * aspect_ratio
            plane_scale_y = 2.0
        
        bpy.ops.mesh.primitive_plane_add(size=1)
        plane = bpy.context.active_object
        plane.scale = (plane_scale_x, plane_scale_y, 1.0)
        
        # Apply the scale to make it permanent
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        
        bpy.context.view_layer.objects.active = plane

        update_progress(80, "Adding geometry detail...")

        # Dense geometry for displacement
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.subdivide(number_cuts=p.subdiv_cuts)
        bpy.ops.object.mode_set(mode='OBJECT')

        update_progress(90, "Setting up materials...")

        # Material with displacement and texture
        mat = bpy.data.materials.new(name="Depth_Displace_Mat")
        mat.use_nodes = True

        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        for n in nodes:
            n.select = False

        # Depth map texture node for displacement
        depth_tex = nodes.new("ShaderNodeTexImage")
        depth_tex.label = "Depth Map"
        depth_tex.interpolation = 'Linear'
        depth_tex.image = depth_image
        depth_tex.location = (-600, -200)

        # Original image texture node for surface color
        original_tex = nodes.new("ShaderNodeTexImage")
        original_tex.label = "Original Image"
        original_tex.interpolation = 'Linear'
        original_tex.image = original_image
        original_tex.location = (-400, 300)

        # Displacement nodes
        sep = nodes.new("ShaderNodeSeparateRGB")
        sep.location = (-400, -200)
        mul = nodes.new("ShaderNodeMath")
        mul.operation = 'MULTIPLY'
        mul.inputs[1].default_value = p.disp_scale
        mul.location = (-200, -200)

        disp = nodes.new("ShaderNodeDisplacement")
        disp.location = (0, -200)
        
        bsdf = nodes.get("Principled BSDF")
        bsdf.location = (200, 300)
        out = nodes.get("Material Output")
        out.location = (400, 0)

        # Link original image to BSDF base color
        links.new(original_tex.outputs["Color"], bsdf.inputs["Base Color"])

        # Link depth map for displacement: color -> separate -> scale -> displacement
        links.new(depth_tex.outputs["Color"], sep.inputs["Image"])
        links.new(sep.outputs["G"], mul.inputs[0])
        links.new(mul.outputs["Value"], disp.inputs["Height"])
        links.new(disp.outputs["Displacement"], out.inputs["Displacement"])
        links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

        # Assign & ensure it's the active material slot
        if plane.data.materials:
            plane.data.materials[0] = mat
        else:
            plane.data.materials.append(mat)
        plane.active_material_index = 0
        plane.active_material = mat

        update_progress(95, "Applying displacement settings...")

        # 🔥 Force "Displacement Only" (true displacement)
        set_displacement_only(plane, mat)

        update_progress(100, "Complete!")

        self.report({'INFO'}, f"Done. Depth map applied with true displacement and original image texture: {Path(depth_png).name}")
        
        # Reset processing state
        p.is_processing = False
        p.progress = 0.0
        
        return {'FINISHED'}

# --------- Panel ----------
class IDepth_PT_Panel(bpy.types.Panel):
    bl_label = "Image → Depth → Mesh"
    bl_idname = "IDEPTH_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Depth Map'

    def draw(self, context):
        l = self.layout
        p = context.scene.idepth

        # Show progress bar if processing
        if p.is_processing:
            progress_box = l.box()
            progress_box.label(text="Processing...", icon='TIME')
            progress_row = progress_box.row()
            progress_row.prop(p, "progress", slider=True, text="")
            l.separator()

        # Paths section with blue theme
        box = l.box()
        header = box.row()
        header.alert = False
        header.label(text="Paths", icon='FILE_FOLDER')
        
        # Image selection with drag & drop hint and recent images
        col = box.column()
        col.scale_y = 0.9
        
        # File path with drag & drop instruction
        row = col.row(align=True)
        row.prop(p, "filepath", text="")
        recent_op = row.operator(IDepth_OT_ToggleRecent.bl_idname, 
                                text="", icon='DOWNARROW_HLT' if p.show_recent else 'RIGHTARROW_THIN')
        
        # Drag & drop hint
        if not p.filepath:
            hint_row = col.row()
            hint_row.scale_y = 0.8
            hint_row.label(text="💡 Drag & drop an image file here", icon='INFO')
        
        # Recent images dropdown
        if p.show_recent and len(p.recent_images) > 0:
            recent_box = col.box()
            recent_box.scale_y = 0.8
            recent_box.label(text="Recent Images:", icon='HISTORY')
            for item in p.recent_images:
                if Path(item.name).exists():
                    row = recent_box.row()
                    row.scale_y = 0.9
                    op = row.operator(IDepth_OT_SelectRecent.bl_idname, 
                                    text=Path(item.name).name, icon='IMAGE_DATA')
                    op.filepath = item.name
        
        col.prop(p, "outdir")
        col.prop(p, "repo_dir")
        col.prop(p, "venv_python")

        l.separator(factor=0.5)

        # Depth Anything V2 options with green theme
        box = l.box()
        header = box.row()
        header.operator_context = 'INVOKE_DEFAULT'
        header.label(text="Depth Anything V2 options", icon='SHADING_RENDERED')
        
        # Encoder selection with color emphasis
        col = box.column()
        col.scale_y = 0.9
        row = col.row(align=True)
        row.scale_y = 1.2
        row.prop(p, "encoder", expand=True)
        
        # Other depth options
        subcol = col.column(align=True)
        subcol.prop(p, "input_size")
        
        # Boolean options in a row with color coding
        row = subcol.row(align=True)
        row.scale_y = 1.1
        sub = row.row(align=True)
        sub.prop(p, "grayscale", toggle=True)
        sub = row.row(align=True) 
        sub.prop(p, "pred_only", toggle=True)

        l.separator(factor=0.5)

        # Mesh/Displacement section with orange theme
        box = l.box()
        header = box.row()
        header.label(text="Mesh / Displacement", icon='MESH_GRID')
        
        col = box.column(align=True)
        col.scale_y = 1.1
        col.prop(p, "subdiv_cuts")
        col.prop(p, "disp_scale")

        l.separator(factor=1.0)
        
        # Main action button with emphasis
        row = l.row()
        row.scale_y = 1.5
        
        # Disable button while processing
        if p.is_processing:
            row.enabled = False
            row.operator(IDepth_OT_Run.bl_idname, icon='TIME', text="Processing...")
        else:
            row.operator(IDepth_OT_Run.bl_idname, icon='MOD_DISPLACE')

# --------- Registration ----------
classes = (
    RecentImageItem,
    IDepthProps,
    IDepth_OT_SelectRecent,
    IDepth_OT_ToggleRecent,
    IDepth_OT_DropImage,
    IDepth_OT_Run,
    IDepth_PT_Panel,
)

def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.idepth = bpy.props.PointerProperty(type=IDepthProps)

def unregister():
    del bpy.types.Scene.idepth
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

if __name__ == "__main__":
    register()