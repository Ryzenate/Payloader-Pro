import azure.functions as func
import logging
import json
import os
import zipfile
import tempfile
import shutil
import re
from pathlib import Path
from azure.storage.blob import BlobServiceClient

app = func.FunctionApp()

@app.route(route="processUpdate", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def processUpdate(req: func.HttpRequest) -> func.HttpResponse:
    try:
        # Parse request
        req_body = req.get_json()
        container_name = req_body.get('containerName')
        source_blob_path = req_body.get('sourceBlobPath')
        
        if not container_name or not source_blob_path:
            return func.HttpResponse("Missing containerName or sourceBlobPath", status_code=400)
        
        # Connect to Azure Storage (use Managed Identity or connection string)
        conn_string = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
        blob_client = BlobServiceClient.from_connection_string(conn_string)
        container_client = blob_client.get_container_client(container_name)
        
        # Create temp working directory
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            download_path = temp_path / "original.zip"
            extract_path = temp_path / "extracted"
            
            # 1. Download the ZIP from blob storage
            logging.info(f"Downloading {source_blob_path} from container {container_name}")
            blob_data = container_client.download_blob(source_blob_path).readall()
            with open(download_path, "wb") as f:
                f.write(blob_data)
            
            # 2. Extract to temporary folder
            extract_path.mkdir()
            with zipfile.ZipFile(download_path, 'r') as zip_ref:
                zip_ref.extractall(extract_path)
            
            # 3. Determine OEM and component from folder path
            #    Example: "ASUS/BIOS/v123/update.zip" -> OEM=ASUS, Component=BIOS
            path_parts = Path(source_blob_path).parts
            oem = "Unknown"
            component = "Unknown"
            
            oem_keywords = ["ASUS", "HP", "Lenovo", "Dell", "Acer", "MSI", "Gigabyte"]
            component_keywords = ["BIOS", "GFX", "Chipset", "NPU", "WLAN", "Audio", "LAN", "SATA"]
            
            for part in path_parts:
                if part.upper() in [k.upper() for k in oem_keywords]:
                    oem = part.upper()
                if part.upper() in [k.upper() for k in component_keywords]:
                    component = part.upper()
            
            # 4. Analyze installer type and find main executable
            installer_info = analyze_installer(extract_path)
            
            # 5. Dynamically create install.bat if missing
            if not installer_info.get('has_install_bat'):
                create_install_bat(extract_path, installer_info)
                installer_info['install_bat_created'] = True
            
            # 6. Create metadata JSON files
            metadirs = {
                "original_blob_path": source_blob_path,
                "extracted_from_folders": path_parts,
                "oem": oem,
                "component": component,
                "timestamp": str(Path(source_blob_path).stat().st_ctime) if hasattr(Path(source_blob_path), 'stat') else "unknown"
            }
            
            zipmanifest = {
                "original_filename": Path(source_blob_path).name,
                "install_type": installer_info['install_type'],
                "installer_file": installer_info['main_installer'],
                "has_install_bat": installer_info['has_install_bat'],
                "install_bat_created": installer_info.get('install_bat_created', False),
                "detected_executables": installer_info['detected_files'],
                "oem": oem,
                "component": component
            }
            
            # Save JSON files in extracted folder
            metadirs_path = extract_path / "metadirs.json"
            zipmanifest_path = extract_path / "zipmanifest.json"
            
            with open(metadirs_path, "w") as f:
                json.dump(metadirs, f, indent=2)
            with open(zipmanifest_path, "w") as f:
                json.dump(zipmanifest, f, indent=2)
            
            # 7. Re-zip everything including the new files and modified structure
            output_zip_name = f"processed_{Path(source_blob_path).stem}.zip"
            output_zip_path = temp_path / output_zip_name
            
            with zipfile.ZipFile(output_zip_path, 'w', zipfile.ZIP_DEFLATED) as new_zip:
                for file_path in extract_path.rglob("*"):
                    arcname = file_path.relative_to(extract_path)
                    new_zip.write(file_path, arcname)
            
            # 8. Upload to /out subfolder in same container
            output_blob_path = f"out/{output_zip_name}"
            with open(output_zip_path, "rb") as f:
                container_client.upload_blob(output_blob_path, f, overwrite=True)
            
            # Also upload the JSON files individually for easy access
            with open(metadirs_path, "rb") as f:
                container_client.upload_blob(f"out/{Path(source_blob_path).stem}_metadirs.json", f, overwrite=True)
            with open(zipmanifest_path, "rb") as f:
                container_client.upload_blob(f"out/{Path(source_blob_path).stem}_zipmanifest.json", f, overwrite=True)
            
            logging.info(f"Successfully processed {source_blob_path}")
            
            return func.HttpResponse(
                json.dumps({
                    "status": "success",
                    "outputFile": output_zip_name,
                    "oem": oem,
                    "component": component,
                    "installType": installer_info['install_type']
                }),
                mimetype="application/json",
                status_code=200
            )
            
    except Exception as e:
        logging.error(f"Error processing update: {str(e)}")
        return func.HttpResponse(f"Error: {str(e)}", status_code=500)


def analyze_installer(extract_path: Path) -> dict:
    """
    Analyzes extracted files to determine installer type and main executable.
    """
    detected_files = []
    main_installer = None
    install_type = "unknown"
    has_install_bat = False
    
    # Look for common installer files across all directories
    for root, dirs, files in os.walk(extract_path):
        for file in files:
            file_lower = file.lower()
            detected_files.append(file)
            
            if file_lower == "install.bat":
                has_install_bat = True
                main_installer = os.path.join(root, file)
                install_type = "batch"
            
            elif file_lower == "setup.exe":
                if not main_installer or install_type != "batch":
                    main_installer = os.path.join(root, file)
                    install_type = "exe_setup"
            
            elif file_lower.endswith(".inf"):
                if install_type == "unknown":
                    main_installer = os.path.join(root, file)
                    install_type = "inf_driver"
            
            elif file_lower.endswith(".msi"):
                if install_type not in ["batch", "exe_setup"]:
                    main_installer = os.path.join(root, file)
                    install_type = "msi"
            
            elif file_lower == "flash.nsh" or file_lower == "flash.bat":
                if "bios" in str(root).lower():
                    main_installer = os.path.join(root, file)
                    install_type = "winflash"
    
    # If only INF files exist (common for chipset/network drivers)
    if install_type == "inf_driver" and not main_installer:
        inf_files = [f for f in detected_files if f.endswith('.inf')]
        if inf_files:
            main_installer = inf_files[0]
    
    return {
        "has_install_bat": has_install_bat,
        "main_installer": main_installer,
        "install_type": install_type,
        "detected_files": list(set(detected_files[:20]))  # Limit to first 20 unique
    }


def create_install_bat(extract_path: Path, installer_info: dict):
    """
    Dynamically create an install.bat file based on detected installer type.
    Saves alongside the extracted files.
    """
    install_bat_path = extract_path / "install.bat"
    install_type = installer_info['install_type']
    main_installer = installer_info['main_installer']
    
    if main_installer:
        # Get relative path from extract_path to the installer
        rel_path = os.path.relpath(main_installer, extract_path)
        rel_path = rel_path.replace("\\", "/")
        
        if install_type == "exe_setup":
            content = f"""@echo off
echo Running setup...
"{rel_path}" /silent /verysilent /norestart
if %errorlevel%==0 (
    echo Installation successful.
) else (
    echo Installation failed with error %errorlevel%.
)
pause
"""
        elif install_type == "inf_driver":
            # Use pnputil for INF drivers
            content = f"""@echo off
echo Installing driver...
pnputil /add-driver "{rel_path}" /install
if %errorlevel%==0 (
    echo Driver installed successfully.
) else (
    echo Driver installation failed.
)
pause
"""
        elif install_type == "msi":
            content = f"""@echo off
echo Installing MSI package...
msiexec /i "{rel_path}" /quiet /norestart
if %errorlevel%==0 (
    echo Installation successful.
) else (
    echo Installation failed.
)
pause
"""
        elif install_type == "winflash":
            content = f"""@echo off
echo Flashing BIOS...
call "{rel_path}"
echo BIOS flash completed. System may reboot.
pause
"""
        else:
            content = f"""@echo off
echo Attempting to run {rel_path}...
"{rel_path}"
if %errorlevel%==0 (
    echo Completed successfully.
) else (
    echo Completed with errors.
)
pause
"""
    else:
        # Fallback: try to run any setup file
        content = f"""@echo off
echo No standard installer detected. Looking for setup files...
for %%f in (setup.exe install.exe update.exe flash.bat) do (
    if exist "%%f" (
        echo Running %%f...
        "%%f"
        goto :done
    )
)
echo No installer found.
:done
pause
"""
    
    with open(install_bat_path, "w") as f:
        f.write(content)