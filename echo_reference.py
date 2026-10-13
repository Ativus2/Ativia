"""Conservative linear echo estimate from the app's own playback samples.

Not OS AEC or speaker identity. The residual still passes the existing VAD.
No recordings/files/network access. Headphone input passes through.
"""
import numpy as np
class EchoReference:
    def filter(self,samples,reference):
        samples=np.asarray(samples,dtype=np.float32)
        if not isinstance(reference,np.ndarray) or reference.size<samples.size:return samples
        # 4 kHz correlation keeps the matching work bounded (<0.8 seconds).
        x=samples[::4];r=reference[::4]
        if len(x)<32 or len(r)<len(x):return samples
        xx=float(np.dot(x,x))
        if xx<1e-10:return samples
        dots=np.correlate(r,x,'valid')
        power=np.convolve(r*r,np.ones(len(x),dtype=np.float32),'valid')
        score=dots/np.sqrt(np.maximum(power*xx,1e-12))
        index=int(np.argmax(np.abs(score)))
        if abs(float(score[index]))<.55:return samples
        aligned=reference[index*4:index*4+len(samples)]
        if len(aligned)!=len(samples):return samples
        scale=float(np.dot(samples,aligned)/max(1e-10,np.dot(aligned,aligned)))
        return samples-scale*aligned
