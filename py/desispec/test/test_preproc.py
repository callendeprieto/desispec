import unittest
import numpy as np

from desispec.preproc import preproc, _parse_sec_keyword

class TestPreProc(unittest.TestCase):
    
    def setUp(self):
        hdr = dict()
        hdr['CAMERA'] = 'b0'
        hdr['DATE-OBS'] = '2018-09-23T08:17:03.988'
        hdr['CCDSEC'] = '[1:200,1:150]'
        hdr['BIASSECA'] = '[1:20,1:80]'
        hdr['DATASECA'] = '[21:110,1:80]'
        hdr['CCDSECA'] =  '[1:90,1:80]'
        hdr['BIASSECB'] = '[221:240,1:80]'
        hdr['DATASECB'] = '[111:220,1:80]'
        hdr['CCDSECB'] =  '[91:200,1:80]'
        hdr['BIASSECC'] = '[1:20,81:150]'
        hdr['DATASECC'] = '[21:110,81:150]'
        hdr['CCDSECC'] =  '[1:90,81:150]'
        hdr['BIASSECD'] = '[221:240,81:150]'
        hdr['DATASECD'] = '[111:220,81:150]'
        hdr['CCDSECD'] =  '[91:200,81:150]'
        self.header = hdr
        self.ny = 150
        self.nx = 200
        self.noverscan = 20
        self.rawimage = np.zeros((self.ny, self.nx+2*self.noverscan))
        self.offset = dict(A=100.0, B=100.5, C=50.3, D=200.4)
        self.gain = dict(A=1.0, B=1.5, C=0.8, D=1.2)
        self.rdnoise = dict(A=2.0, B=2.2, C=2.4, D=2.6)
        
        self.quad = dict(
            A = np.s_[0:80, 0:90], B = np.s_[0:80, 90:200],
            C = np.s_[80:150, 0:90], D = np.s_[80:150, 90:200],
        )
        
        for amp in ('A', 'B', 'C', 'D'):
            self.header['GAIN'+amp] = self.gain[amp]
            self.header['RDNOISE'+amp] = self.rdnoise[amp]
            
            xy = _parse_sec_keyword(hdr['BIASSEC'+amp])
            shape = [xy[0].stop-xy[0].start, xy[1].stop-xy[1].start]
            self.rawimage[xy] += self.offset[amp]
            self.rawimage[xy] += np.random.normal(scale=self.rdnoise[amp], size=shape)/self.gain[amp]
            xy = _parse_sec_keyword(hdr['DATASEC'+amp])
            shape = [xy[0].stop-xy[0].start, xy[1].stop-xy[1].start]
            self.rawimage[xy] += self.offset[amp]
            self.rawimage[xy] += np.random.normal(scale=self.rdnoise[amp], size=shape)/self.gain[amp]

        #- Confirm that all regions were correctly offset
        assert not np.any(self.rawimage == 0.0)
            
    def test_preproc(self):
        image = preproc(self.rawimage, self.header)
        self.assertEqual(image.pix.shape, (self.ny, self.nx))
        self.assertTrue(np.all(image.ivar <= 1/image.readnoise**2))
        for amp in ('A', 'B', 'C', 'D'):
            pix = image.pix[self.quad[amp]]
            rdnoise = np.median(image.readnoise[self.quad[amp]])
            self.assertAlmostEqual(np.median(pix), 0.0, delta=0.2)
            self.assertAlmostEqual(np.std(pix), self.rdnoise[amp], delta=0.2)
            self.assertAlmostEqual(rdnoise, self.rdnoise[amp], delta=0.2)
        
                
if __name__ == '__main__':
    unittest.main()
