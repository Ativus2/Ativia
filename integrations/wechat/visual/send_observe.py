"""Input-free send inspection, only inside an already bound visual worker."""
import ctypes
from ctypes import wintypes as W
from .layout import OCRRow, detect_layout
from .self_capture import find_self_candidates
from .types import Rect, TARGET_GROUP


def composer_region(image, dpi):
    # Recompute from current screenshot separators and current physical DPI.
    layout=detect_layout(image)
    scale=dpi/96
    region=layout.input
    x=region.x+round(12*scale)
    # The current WeChat composer toolbar is at the BOTTOM. Keep the
    # first editable line; excluding a supposed top toolbar can hide a draft.
    y=region.y+round(4*scale)
    # Measured at 175% DPI: the rounded border starts 41 physical pixels
    # from the right client edge. Keep the text interior out of that border.
    # OCR touching any crop edge is rejected by inspect_composer below.
    width=region.right-round(24*scale)-x
    # The enabled Send button begins at y=1050 in the measured 1131px
    # client (175% DPI). A 46-DIP footer included its top green row;
    # leave padding above the toolbar in both empty and nonempty states.
    height=image.height-y-round(50*scale)
    if width<100 or height<35:
        raise ValueError('composer_region_unavailable')
    return Rect(x,y,width,height),layout


def inspect_composer(image, region, rows, dpi, *, allow_caret=False):
    """Empty requires both no OCR and a uniformly blank surface, apart from caret."""
    pixels=image.crop((region.x,region.y,region.right,region.bottom)).convert('RGB')
    colors=pixels.getcolors(pixels.width*pixels.height)
    if not colors:
        raise ValueError('composer_pixel_budget')
    _,background=max(colors)
    # V1 only accepts the current measured dark theme, never guesses a new one.
    if max(abs(background[i]-(30,30,31)[i]) for i in range(3))>6:
        return dict(input_empty=False,input_text='',input_complete=False,ink_pixels=-1)
    ink=[]
    for y in range(pixels.height):
        for x in range(pixels.width):
            if max(abs(pixels.getpixel((x,y))[i]-background[i]) for i in range(3))>12:
                ink.append((x,y))
    visible=[r for r in rows if r.text.strip()]
    contained=lambda r:region.x<r.box.x and region.y<r.box.y and r.box.right<region.right and r.box.bottom<region.bottom
    good=bool(visible) and all(contained(r) and r.score>=.9 for r in visible)
    text='\n'.join(r.text for r in sorted(visible,key=lambda r:(r.box.y,r.box.x)))
    caret=False
    if ink:
        xs,ys=zip(*ink)
        caret=(max(xs)-min(xs)<=max(3,round(dpi/96*2)) and max(ys)-min(ys)<=round(dpi/96*35)
               and max(xs)<round(dpi/96*20))
    # Before focus even a one-character unrecognized draft can resemble a
    # caret. Permit it only after a proven empty PREPARE and our own click.
    empty=not visible and (not ink or allow_caret and caret)
    # Reject unrecognized pixels outside OCR boxes, except a narrow text caret.
    unexplained=[]
    if good:
        for x,y in ink:
            gx,gy=x+region.x,y+region.y
            if not any(r.box.x-4<=gx<=r.box.right+4 and r.box.y-4<=gy<=r.box.bottom+4 for r in visible):
                unexplained.append((x,y))
        if unexplained:
            xs,ys=zip(*unexplained)
            good=max(xs)-min(xs)<=max(3,round(dpi/96*2)) and max(ys)-min(ys)<=round(dpi/96*35)
    return dict(input_empty=empty,input_text=text if good else '',input_complete=good,ink_pixels=len(ink))


def focused_in_target(windows, hwnd):
    class GUI(ctypes.Structure):
        _fields_=[('cbSize',W.DWORD),('flags',W.DWORD),('hwndActive',W.HWND),('hwndFocus',W.HWND),
                  ('hwndCapture',W.HWND),('hwndMenuOwner',W.HWND),('hwndMoveSize',W.HWND),('hwndCaret',W.HWND),('rcCaret',W.RECT)]
    info=GUI();info.cbSize=ctypes.sizeof(info)
    windows.u.GetGUIThreadInfo.argtypes=[W.DWORD,ctypes.POINTER(GUI)]
    if not windows.u.GetGUIThreadInfo(0,ctypes.byref(info)) or info.flags&0x1E:
        return False
    return int(windows.u.GetForegroundWindow() or 0)==hwnd and int(windows.u.GetAncestor(info.hwndFocus,2) or 0)==hwnd


def send_observation(image, current, header, request, ocr, windows, observed_at):
    region,layout=composer_region(image,current['dpi'])
    rows=tuple(OCRRow.from_mapping(r) for r in ocr.recognize(image,(region.x,region.y,region.width,region.height)))
    focused=focused_in_target(windows,current['hwnd'])
    composer=inspect_composer(image,region,rows,current['dpi'],
                              allow_caret=focused and request['phase'] in ('FOCUSED','VERIFY'))
    m=layout.message
    body=tuple(OCRRow.from_mapping(r) for r in ocr.recognize(image,(m.x,m.y,m.width,m.height)))
    own=find_self_candidates(image,layout,body)
    return dict(request_id=request['request_id'],transaction_id=request['transaction_id'],
                binding_id=request['binding_id'],capture_session_id=request['capture_session_id'],capture_epoch=request['capture_epoch'],
                group=TARGET_GROUP,header_exact=header.exact_match,window=current,observed_at=observed_at,
                composer=[region.x,region.y,region.width,region.height],**composer,
                input_focused=focused,
                own_bubbles=[dict(text=c.text,box=list(c.box),side='right',complete=True,confidence=c.confidence) for c in own.candidates])
