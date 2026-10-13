"""Version-pinned MaaFramework free-text OCR, in-memory only and without actions."""
import numpy as np
from .capture import wait_job

class LocalOCR:
    def __init__(self,resource_root,provider):
        from maa.tasker import Tasker
        from maa.resource import Resource
        from maa.define import LoggingLevelEnum
        self.tasker=Tasker()
        for result in (Tasker.set_log_dir(''),Tasker.set_save_draw(False),Tasker.set_save_on_error(False),Tasker.set_debug_mode(False),Tasker.set_stdout_level(LoggingLevelEnum.Off)):
            if not result:raise RuntimeError('OCR privacy options rejected')
        self.resource=Resource();self.resource.set_cpu()
        wait_job(self.resource.post_bundle(resource_root),20)
        self._controller=provider._controller
        if self._controller is None:
            # Tasker needs a connected controller even for supplied in-memory images.
            # This holder uses GDI/Null and never dispatches capture or input jobs.
            from maa.controller import Win32Controller
            from maa.define import MaaWin32ScreencapMethodEnum as S,MaaWin32InputMethodEnum as I
            self._controller=Win32Controller(provider.identity.hwnd,screencap_method=S.GDI,mouse_method=I.Null,keyboard_method=I.Null)
            wait_job(self._controller.post_connection())
        if not self.tasker.bind(self.resource,self._controller) or not self.tasker.inited:raise RuntimeError('OCR_not_initialized')
    def recognize(self,image,roi=None):
        from maa.pipeline import JOCR
        array=np.asarray(image.convert('RGB'))[:,:,::-1].copy();h,w=array.shape[:2]
        box=roi or (0,0,w,h)
        if len(box)!=4 or min(box)<0 or box[0]+box[2]>w or box[1]+box[3]>h:raise ValueError('OCR_ROI_out_of_bounds')
        detail=wait_job(self.tasker.post_recognition('OCR',JOCR(expected=[],roi=box,threshold=.50,only_rec=False),array),20).get()
        rows=[]
        if detail is None:return rows
        for node in detail.nodes:
            if node.recognition:
                for result in node.recognition.filtered_results:
                    text=result.text
                    if isinstance(text,str) and len(text)<=4000:
                        rows.append(dict(text=text,box=list(result.box),score=float(result.score)))
        return rows
