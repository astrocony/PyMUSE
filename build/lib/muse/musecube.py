import copy
import gc
import glob
import math as m
import os

import aplpy
import numpy as np
import numpy.ma as ma
import pyregion
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.utils import isiterable
from linetools.spectra.xspectrum1d import XSpectrum1D
from linetools.utils import name_from_coord
from matplotlib import pyplot as plt
from scipy import interpolate
from scipy import ndimage


# spec = XSpectrum1D.from


class MuseCube:
    """
    Class to handle VLT/MUSE data

    """

    def __init__(self, filename_cube, filename_white, pixelsize=0.2 * u.arcsec, n_fig=1,
                 flux_units=1E-20 * u.erg / u.s / u.cm ** 2 / u.angstrom, vmin=0, vmax=5):
        """
        Parameters
        ----------
        filename_cube: string
            Name of the MUSE datacube .fits file
        filename_white: string
            Name of the MUSE white image .fits file
        pixel_size : float or Quantity, optional
            Pixel size of the datacube, if float it assumes arcsecs.
            Default is 0.2 arcsec
        n_fig : int, optional
            XXXXXXXX
        flux_units : Quantity
            XXXXXXXXXX

        """

        # init
        self.vmin = vmin
        self.vmax = vmax
        self.flux_units = flux_units
        self.n = n_fig
        plt.close(self.n)
        self.filename = filename_cube
        self.filename_white = filename_white
        self.load_data()
        self.white_data = fits.open(self.filename_white)[1].data
        self.gc2 = aplpy.FITSFigure(self.filename_white, figure=plt.figure(self.n))
        self.gc2.show_grayscale(vmin=self.vmin, vmax=self.vmax)
        self.gc = aplpy.FITSFigure(self.filename, slices=[1], figure=plt.figure(20))
        self.pixelsize = pixelsize
        gc.enable()
        plt.close(20)
        print("MuseCube: Ready!")

    def load_data(self):
        hdulist = fits.open(self.filename)
        print("MuseCube: Loading the cube fluxes and variances...")

        # import pdb; pdb.set_trace()
        self.cube = ma.MaskedArray(hdulist[1].data)
        self.stat = ma.MaskedArray(hdulist[2].data)

        print("MuseCube: Defining master masks (this may take a while but it is for the greater good).")
        # masking
        self.mask_init = np.isnan(self.cube) | np.isnan(self.stat)
        self.cube.mask = self.mask_init
        self.stat.mask = self.mask_init

        # for ivar weighting ; consider creating it in init ; takes long
        # self.flux_over_ivar = self.cube / self.stat

        # wavelength array
        self.wavelength = self.create_wavelength_array()

    def smooth_white(self, npix=2, write_to_disk=True):

        hdulist = fits.open(self.filename_white)
        im = hdulist[1].data
        smooth_im = ndimage.gaussian_filter(im, sigma=npix)
        if write_to_disk:
            hdulist[1].data = smooth_im
            hdulist.writeto('smoothed_white.fits', clobber=True)
        return smooth_im

    def create_new_stat(self):
        """Creates a new variance using simple algorithms"""
        median_var_cube = np.zeros_like(self.cube)
        for wv_ii in np.arange(len(self.wavelength)):
            median_ii = np.median(self.stat[wv_ii])
            median_var_cube[wv_ii, :, :] = median_ii
        self.new_var = self.cube + median_var_cube

    def spatial_smooth(self, npix, output="smoothed.fits", test=False, **kwargs):
        """Applies Gaussian filter of std=npix in both spatial directions
        and writes it to disk as a new MUSE Cube.
        Notes: the STAT cube is not touched.

        Parameters
        ----------
        npix : int
            Std of Gaussian kernel in spaxel units.
        output : str, optional
            Name of the output file
        test : bool, optional
            Whether to check for flux being conserved

        **kwargs are passed down to scipy.ndimage.gaussian_filter()

        Return
        ------
        Writes a new file to disk.

        """
        if not isinstance(npix, int):
            raise ValueError("npix must be integer.")

        cube_new = copy.deepcopy(self.cube)
        ntot = len(self.cube)
        for wv_ii in range(ntot):
            print('{}/{}'.format(wv_ii + 1, ntot))
            image_aux = self.cube[wv_ii, :, :]
            smooth_ii = ma.MaskedArray(ndimage.gaussian_filter(image_aux, sigma=npix, **kwargs))
            smooth_ii.mask = image_aux.mask | np.isnan(smooth_ii)

            # test the fluxes are conserved
            if test:
                gd_pix = ~smooth_ii.mask
                try:
                    med_1 = np.nansum(smooth_ii[gd_pix])
                    med_2 = np.nansum(image_aux[gd_pix])
                    print(med_1, med_2, (med_1 - med_2) / med_1)
                    np.testing.assert_allclose(med_1, med_2, decimal=4)
                except AssertionError:
                    import pdb
                    pdb.set_trace()
            cube_new[wv_ii, :, :] = smooth_ii
            # import pdb; pdb.set_trace()

        hdulist = fits.open(self.filename)
        hdulist[1].data = cube_new.data
        prihdr = hdulist[0].header
        comment = 'Spatially smoothed with a Gaussian kernel of sigma={} spaxels (by MuseCube)'.format(npix)
        print(comment)
        prihdr['history'] = comment
        hdulist.writeto(output, clobber=True)
        print("MuseCube: new smoothed cube written to {}".format(output))

    def get_mini_image(self, center, halfsize=15):

        """

        :param center: tuple of coordinates, in pixels
        :param size: length of the square around center
        :return: ndarray which contain the image
        """
        side = 2 * halfsize + 1
        image = [[0 for x in range(side)] for y in range(side)]
        data_white = fits.open(self.filename_white)[1].data
        center_x = center[0]
        center_y = center[1]
        for i in xrange(center_x - halfsize, center_x + halfsize + 1):
            for j in xrange(center_y - halfsize, center_y + halfsize + 1):
                i2 = i - (center_x - halfsize)
                j2 = j - (center_y - halfsize)
                image[j2][i2] = data_white[j][i]
        return image

    def get_weighted_spec_circular_aperture(self, x_c, y_c, radius, nsig=4):
        import scipy.ndimage.filters as fi
        new_3dmask = self.get_mini_cube_mask_from_ellipse_params(x_c, y_c, radius)
        w = self.wavelength
        n = len(w)
        fl = np.zeros(n)
        sig = np.zeros(n)
        self.cube.mask = new_3dmask
        for wv_ii in range(n):
            mask = new_3dmask[wv_ii]
            center = np.zeros(mask.shape)  ###Por alguna razon no funciona si cambio la asignacion a np.zeros_lime(mask)
            center[y_c][x_c] = 1
            weigths = ma.MaskedArray(fi.gaussian_filter(center, nsig))
            weigths.mask = mask
            weigths = weigths / np.sum(weigths)
            fl[wv_ii] = np.sum(self.cube[wv_ii] * weigths)
            sig[wv_ii] = np.sqrt(np.sum(self.stat[wv_ii] * (weigths ** 2)))
        self.cube.mask = self.mask_init
        return XSpectrum1D.from_tuple((w, fl, sig))

    def get_spec_spaxel(self, x, y, coord_system='pix'):
        """
        Gets the spectrum of a single spaxel (xy) of the MuseCube
        :param x: x coordinate of the spaxel
        :param y: y coordinate of the spaxel
        :param coord_system: 'pix' or 'wcs'
        :return: XSpectrum1D object
        """
        if coord_system == 'wcs':
            x_c, y_c = self.w2p(x, y)
        else:
            x_c, y_c = x, y
        region_string = self.ellipse_param_to_ds9reg_string(x_c, y_c, 1, 1, 0, coord_system='pix')
        self.draw_pyregion(region_string)
        w = self.wavelength
        n = len(w)
        spec = np.zeros(n)
        sigma = np.zeros(n)
        for wv_ii in range(n):
            spec[wv_ii] = self.cube.data[wv_ii][int(y_c)][int(x_c)]
            sigma[wv_ii] = self.stat.data[wv_ii][int(y_c)][int(x_c)]
        return XSpectrum1D.from_tuple((self.wavelength, spec, sigma))

    def get_spec_from_ellipse_params(self, x_c, y_c, params, coord_system='pix', mode='optimal', n_figure=2, save=False):
        """Obtains a combined spectrum of spaxels within a geometrical region defined by
        x_c, y_c, params."""
        if mode == 'optimal':
            spec = self.get_weighted_spec(x_c=x_c,y_c=y_c,params=params)
        else:
            new_mask = self.get_mini_cube_mask_from_ellipse_params(x_c, y_c, params, coord_system=coord_system)
            spec = self.spec_from_minicube_mask(new_mask, mode=mode)
        plt.figure(n_figure)
        plt.plot(spec.wavelength, spec.flux)
        if coord_system == 'wcs':
            x_world, y_world = x_c, y_c
        else:
            x_world, y_world = self.p2w(x_c, y_c)
        coords = SkyCoord(ra=x_world, dec=y_world, frame='icrs', unit='deg')
        name = name_from_coord(coords)
        plt.title(name)
        plt.xlabel('Angstroms')
        plt.ylabel('Flux (' + str(self.flux_units) + ')')
        if save:
            spec.write_to_fits(name + '.fits')
        return spec

    def get_spec_from_interactive_polygon_region(self, mode='mean',
                                                 n_figure=2):  ##Se necesita inicializar como ipython --pylab qt
        """
        Function used to interactively define a region and extract the spectrum of that region

        To use this function, the class must have been initialized in a "ipython --pylab qt" enviroment
        It's also needed the package roipoly. Installation instructions and LICENSE in:
        https://github.com/jdoepfert/roipoly.py/

        :param mode: type of combination for fluxes. Possible values: mean, median, ivar
        :param n_figure: figure to display the spectrum
        :return: spec: XSpectrum1D ob
        """
        from roipoly import roipoly
        current_fig = plt.figure(self.n)
        MyROI = roipoly(roicolor='r', fig=current_fig)
        raw_input("MuseCube: Please select points with left click. Right click and Enter to continue...")
        print("MuseCube: Calculating the spectrum...")
        mask = MyROI.getMask(self.white_data)
        mask_inv = np.where(mask == 1, 0, 1)
        complete_mask = self.mask_init + mask_inv
        new_3dmask = np.where(complete_mask == 0, False, True)
        spec = self.spec_from_minicube_mask(new_3dmask, mode=mode)
        self.clean_canvas()
        plt.figure(n_figure)
        plt.plot(spec.wavelength, spec.flux)
        plt.ylabel('Flux (' + str(self.flux_units) + ')')
        plt.xlabel('Wavelength (Angstroms)')
        plt.title('Polygonal region spectrum ')
        plt.figure(self.n)
        MyROI.displayROI()
        return spec

    def params_from_ellipse_region_string(self, region_string,deg=False):
        r = pyregion.parse(region_string)
        if r[0].coord_format == 'physical' or r[0].coord_format == 'image':
            x_c, y_c, params = r[0].coord_list[0], r[0].coord_list[1], r[0].coord_list[2:5]
        else:
            x_world = r[0].coord_list[0]
            y_world = r[0].coord_list[1]
            par = r[0].coord_list[2:5]
            x_c, y_c, params = self.ellipse_params_to_pixel(x_world, y_world, par)
        if deg:
            x_world,y_world=self.p2w(x_c-1,y_c-1)
            return x_world,y_world

        return x_c-1, y_c-1,params

    def get_spec_from_region_string(self, region_string, mode='optimal', n_figure=2, save=False):
        """
        Obtains a combined spectru of spaxel within geametrical region defined by the region _string, interpretated by ds9
        :param region_string: str
                              String defining a ds9 region
        :param mode: string. default = ivar
        :return: spec: XSpectrum1D object.
        """
        if mode =='optimal':
            spec = self.get_weighted_spec(region_string_=region_string)
        else:
            new_mask = self.get_mini_cube_mask_from_region_string(region_string)
            spec = self.spec_from_minicube_mask(new_mask, mode=mode)
        plt.figure(n_figure)
        plt.plot(spec.wavelength, spec.flux)
        x_world, y_world = self.params_from_ellipse_region_string(region_string,deg=True)
        coords = SkyCoord(ra=x_world, dec=y_world, frame='icrs', unit='deg')
        name = name_from_coord(coords)
        plt.title(name)
        plt.xlabel('Angstroms')
        plt.ylabel('Flux (' + str(self.flux_units) + ')')
        if save:
            spec.write_to_fits(name + '.fits')
        return spec

    def draw_pyregion(self, region_string):
        hdulist = fits.open(self.filename_white)
        r = pyregion.parse(region_string).as_imagecoord(hdulist[1].header)
        fig = plt.figure(self.n)
        ax = fig.axes[0]
        patch_list, artist_list = r.get_mpl_patches_texts()
        patch = patch_list[0]
        ax.add_patch(patch)

    def spec_from_minicube_mask(self, new_3dmask, mode='mean'):
        """Given a 3D mask, this function provides a combined spectrum
        of all non-masked voxels.

        Parameters
        ----------
        new_3dmask : np.array of same shape as self.cube
            The 3D mask
        mode : str
            Mode for combining spaxels:
              * `ivar` - inverse variance weighting
              * `sum` - Sum
              * `optimal` - Weighted sum by spatial profile (only works for circular thus far)

        Returns
        -------
        An XSpectrum1D object (from linetools) with the combined spectrum.

        """
        if mode not in ['ivar', 'mean', 'median']:
            raise ValueError("Not ready for this type of `mode`.")
        if np.shape(new_3dmask) != np.shape(self.cube.mask):
            raise ValueError("new_3dmask must be of same shape as the original MUSE cube.")

        n = len(self.wavelength)
        fl = np.zeros(n)
        er = np.zeros(n)
        # I think we can avoid redefining the masks
        # self.cube.mask=new_3dmask
        # self.stat.mask=new_3dmask

        for wv_ii in xrange(n):
            mask = new_3dmask[wv_ii]  # 2-D mask
            mask = np.where(mask != 0, True, False)  # not sure if this is needed
            im_fl = self.cube[wv_ii][~mask]  # this is a 1-d np.array()
            im_var = self.stat[wv_ii][~mask]  # this is a 1-d np.array()
            if mode == 'ivar':
                flux_ivar = im_fl / im_var
                fl[wv_ii] = np.sum(flux_ivar) / np.sum(1. / im_var)
                er[wv_ii] = np.sqrt(np.sum(1. / im_var))
            elif mode == 'mean':
                fl[wv_ii] = np.mean(im_fl)
                er[wv_ii] = np.sqrt(np.sum(im_var)) / len(im_fl)
            elif mode == 'median':
                fl[wv_ii] = np.median(im_fl)
                er[wv_ii] = 1.2533 * np.sqrt(np.sum(im_var)) / len(im_fl)
        # import pdb;pdb.set_trace()
        return XSpectrum1D.from_tuple((self.wavelength, fl, er))

    def get_spec_image(self, center, halfsize=15, n_fig=3, mode='optimal', coord_system='pix'):

        """
        Function to Get a spectrum and an image of the selected source.

        :param center: Tuple. Contain the coordinates in pixels of the center
        :param halfsize: int or list. If int, is the halfsize of the image box and the radius of a circular aperture to get the spectrum
                                      If list, contain the [a,b,alpha] parameter for an eliptical aperture. The box will be a square with the major semiaxis
        :param n_fig: Figure number to deploy the image
        :return:
        """
        spectrum = self.get_spec_from_ellipse_params(x_c=center[0], y_c=center[1], params=halfsize,
                                                     coord_system=coord_system, mode=mode)
        if isinstance(halfsize, (list, tuple)):
            aux = [halfsize[0], halfsize[1]]
            halfsize = max(aux)
        mini_image = self.get_mini_image(center=center, halfsize=halfsize)
        plt.figure(n_fig, figsize=(17, 5))
        ax1 = plt.subplot2grid((1, 4), (0, 0), colspan=3)
        if coord_system == 'pix':
            x_world, y_world = self.p2w(center[0], center[1])
        else:
            x_world, y_world = center[0], center[1]
        coord = SkyCoord(ra=x_world, dec=y_world, frame='icrs', unit='deg')
        spec_name = name_from_coord(coord)
        plt.title(spec_name)
        w = spectrum.wavelength.value
        f = spectrum.flux.value
        ax1.plot(w, f)
        plt.ylabel('Flux (' + str(self.flux_units) + ')')
        plt.xlabel('Wavelength (Angstroms)')
        n = len(w)
        ave = np.nanmean(f)
        std = np.nanstd(f)
        ymin = ave - 3 * std
        ymax = ave + 4 * std
        plt.ylim([ymin, ymax])
        plt.xlim([w[0], w[n - 1]])
        ax2 = plt.subplot2grid((1, 4), (0, 3), colspan=1)
        ax2.imshow(mini_image, cmap='gray')
        plt.ylim([0, 2 * halfsize])
        plt.xlim([0, 2 * halfsize])
        return spectrum

    def create_wavelength_array(self):
        """
        Creates the wavelength array for the spectrum. The values of dw, and limits will depend
        of the data and should be revised.
        :return: w: array[]
                 array which contain an evenly sampled wavelength range
        """
        hdulist = fits.open(self.filename)
        header = hdulist[1].header
        dw = header['CD3_3']
        w_ini = header['CRVAL3']
        N = header['NAXIS3']
        w_fin = w_ini + (N - 1) * dw
        # w_aux = w_ini + dw*np.arange(0, N) #todo: check whether w_aux and w are the same
        w = np.linspace(w_ini, w_fin, N)
        # print 'wavelength in range ' + str(w[0]) + ' to ' + str(w[len(w) - 1]) + ' and dw = ' + str(dw)
        return w

    def closest_element(self, array, element):
        """
        Find in array the closest value to the value of element
        :param array: array[]
                      array of numbers to search for the closest value possible to element
        :param element: float
                        element to match with array
        :return: k: int
                    index of the closest element
        """
        min_dif = 100
        index = -1
        n = len(array)
        for i in xrange(0, n):
            dif = abs(element - array[i])
            if dif < min_dif:
                index = i
                min_dif = dif
        return int(index)

    def __cut_bright_pixel(self, flux, n):
        '''
        cuts the brightest n elements in a sorted flux array

        :param flux: array[]
                     array containing the sorted flux
        :param n: int
                  number of elements to cut
        :return: cuted_flux: array[]
                             array that contains the remaining elements of the flux
        '''
        N = len(flux)
        cuted_flux = []
        for i in xrange(0, N - n):
            cuted_flux.append(flux[i])
        return cuted_flux

    def __edit_header(self, hdulist, values_list,
                      keywords_list=['CRPIX1', 'CRPIX2', 'CD1_1', 'CD2_2', 'CRVAL1', 'CRVAL2'], hdu=1):
        hdu_element = hdulist[hdu]
        if len(keywords_list) != len(values_list):
            raise ValueError('Dimensions of keywords_list and values-list does not match')
        n = len(values_list)
        for i in xrange(n):
            keyword = keywords_list[i]
            value = values_list[i]
            hdu_element.header[keyword] = value
        # CSYER1=hdu_element.header['CSYER1']
        # hdu_element.header['CSYER1']=1000.0860135214331
        hdulist_edited = hdulist
        hdulist_edited[hdu] = hdu_element
        return hdulist_edited

    def __save2fits(self, fitsname, data_to_save, stat=False, type='cube', n_figure=2, edit_header=[]):
        if type == 'white':
            hdulist = fits.HDUList.fromfile(self.filename_white)
            hdulist[1].data = data_to_save
            if len(edit_header) == 0:
                hdulist.writeto(fitsname, clobber=True)
                im = aplpy.FITSFigure(fitsname, figure=plt.figure(n_figure))
                im.show_grayscale()
            elif len(edit_header) == 1:
                values_list = edit_header[0]
                hdulist_edited = self.__edit_header(hdulist, values_list=values_list)
                hdulist_edited.writeto(fitsname, clobber=True)
                im = aplpy.FITSFigure(fitsname, figure=plt.figure(n_figure))
                im.show_grayscale()
            elif len(edit_header) == 2:
                values_list = edit_header[0]
                keywords_list = edit_header[1]
                hdulist_edited = self.__edit_header(hdulist, values_list=values_list, keywords_list=keywords_list)
                hdulist_edited.writeto(fitsname, clobber=True)
                im = aplpy.FITSFigure(fitsname, figure=plt.figure(n_figure))
                im.show_grayscale()
            elif len(edit_header) == 3:
                values_list = edit_header[0]
                keywords_list = edit_header[1]
                hdu = edit_header[2]
                hdulist_edited = self.__edit_header(hdulist, values_list=values_list, keywords_list=keywords_list,
                                                    hdu=hdu)
                hdulist_edited.writeto(fitsname, clobber=True)
                im = aplpy.FITSFigure(fitsname, figure=plt.figure(n_figure))
                im.show_grayscale()

        if type == 'cube':
            hdulist = fits.HDUList.fromfile(self.filename)
            if stat == False:
                hdulist[1].data = data_to_save
            if stat == True:
                hdulist[2].data = data_to_save
            if len(edit_header) == 0:
                hdulist.writeto(fitsname, clobber=True)
                im = aplpy.FITSFigure(fitsname, slices=[1], figure=plt.figure(n_figure))
                im.show_grayscale()
            elif len(edit_header) == 1:
                values_list = edit_header[0]
                hdulist_edited = self.__edit_header(hdulist, values_list=values_list)
                hdulist_edited.writeto(fitsname, clobber=True)
                im = aplpy.FITSFigure(fitsname, slices=[1], figure=plt.figure(n_figure))
                im.show_grayscale()
            elif len(edit_header) == 2:
                values_list = edit_header[0]
                keywords_list = edit_header[1]
                hdulist_edited = self.__edit_header(hdulist, values_list=values_list, keywords_list=keywords_list)
                hdulist_edited.writeto(fitsname, clobber=True)
                im = aplpy.FITSFigure(fitsname, slices=[1], figure=plt.figure(n_figure))
                im.show_grayscale()
            elif len(edit_header) == 3:
                values_list = edit_header[0]
                keywords_list = edit_header[1]
                hdu = edit_header[2]
                hdulist_edited = self.__edit_header(hdulist, values_list=values_list, keywords_list=keywords_list,
                                                    hdu=hdu)
                hdulist_edited.writeto(fitsname, clobber=True)
                im = aplpy.FITSFigure(fitsname, slices=[1], figure=plt.figure(n_figure))
                im.show_grayscale()

    def ellipse_params_to_pixel(self, xc, yc, radius):
        a = radius[0]
        b = radius[1]
        Xaux, Yaux, a2 = self.xyr_to_pixel(xc, yc, a)
        xc2, yc2, b2 = self.xyr_to_pixel(xc, yc, b)
        radius2 = [a2, b2, radius[2]]
        return xc2, yc2, radius2

    def define_elipse_region(self, x_center, y_center, a, b, theta, coord_system):
        Xc = x_center
        Yc = y_center
        if coord_system == 'wcs':
            X_aux, Y_aux, a = self.xyr_to_pixel(Xc, Yc, a)
            Xc, Yc, b = self.xyr_to_pixel(Xc, Yc, b)
        if b > a:
            aux = a
            a = b
            b = aux
            theta = theta + np.pi / 2.
        x = np.linspace(-a, a, 5000)

        B = x * np.sin(2. * theta) * (a ** 2 - b ** 2)
        A = (b * np.sin(theta)) ** 2 + (a * np.cos(theta)) ** 2
        C = (x ** 2) * ((b * np.cos(theta)) ** 2 + (a * np.sin(theta)) ** 2) - (a ** 2) * (b ** 2)

        y_positive = (-B + np.sqrt(B ** 2 - 4. * A * C)) / (2. * A)
        y_negative = (-B - np.sqrt(B ** 2 - 4. * A * C)) / (2. * A)

        y_positive_2 = y_positive + Yc
        y_negative_2 = y_negative + Yc
        x_2 = x + Xc
        reg = []
        for i in xrange(Xc - a - 1, Xc + a + 1):
            for j in xrange(Yc - a - 1, Yc + a + 1):
                k = self.closest_element(x_2, i)
                if j <= y_positive_2[k] and j >= y_negative_2[k]:
                    reg.append([i, j])
        return reg

    def get_mini_cube_mask_from_region_string(self, region_string):
        """
        Creates a 3D mask where all original masked voxels are masked out,
        plus all voxels associated to spaxels outside the elliptical region
        defined by the given parameters.
        :param region_string: Region defined by ds9 format
        :return: complete_mask_new: a new mask for the cube
        """
        complete_mask_new = self.create_new_3dmask(region_string)
        return complete_mask_new

    def get_mini_cube_mask_from_ellipse_params(self, x_c, y_c, params, coord_system='pix'):
        """
        Creates a 3D mask where all original masked voxels are masked out,
        plus all voxels associated to spaxels outside the elliptical region
        defined by the given parameters.

        :param x_c: center of the elliptical aperture
        :param y_c: center of the elliptical aperture
        :param params: can be a single radius (float) of an circular aperture, or a (a,b,theta) tuple
        :param coord_system: default: pix, possible values: pix, wcs
        :return: complete_mask_new: a new mask for the cube
        """

        if not isinstance(params, (int, float, tuple, list, np.array)):
            raise ValueError('Not ready for this `radius` type.')

        if isinstance(params, (int, float)):
            a = params
            b = params
            theta = 0
        elif isiterable(params) and (len(params) == 3):
            a = max(params[:2])
            b = min(params[:2])
            theta = params[2]
        else:
            raise ValueError('If iterable, the length of radius must be == 3; otherwise try float.')

        region_string = self.ellipse_param_to_ds9reg_string(x_c, y_c, a, b, theta, coord_system=coord_system)
        complete_mask_new = self.create_new_3dmask(region_string)
        return complete_mask_new

    def ellipse_param_to_ds9reg_string(self, xc, yc, a, b, theta, color='green', coord_system='pix'):
        """Creates a string that defines an elliptical region given by the
        parameters using the DS9 convention.
        """
        if coord_system == 'wcs':
            x_center, y_center, radius = self.ellipse_params_to_pixel(xc, yc, radius=[a, b, theta])
        else:  # already in pixels
            x_center, y_center, radius = xc, yc, [a, b, theta]
        region_string = 'physical;ellipse({},{},{},{},{}) # color = {}'.format(x_center + 1, y_center + 1, radius[0],
                                                                               radius[1],
                                                                               radius[2], color)
        return region_string

    def _test_3dmask(self, region_string, alpha=0.8, slice=0):
        complete_mask = self.create_new_3dmask(region_string)
        mask_slice = complete_mask[int(slice)]
        plt.figure(self.n)
        plt.imshow(mask_slice, alpha=alpha)
        self.draw_pyregion(region_string)

    def create_new_3dmask(self, region_string, _2d=False):
        """Creates a 3D mask for the cube that also mask out
        spaxels that are outside the gemoetrical redion defined by
        region_string.

        Parameters
        ----------
        region_string : str
            A string that defines a geometrical region using the
            DS9 format (e.g. see http://ds9.si.edu/doc/ref/region.html)

        Returns
        -------
        A 3D mask that includes already masked voxels from the original cube,
        plus all spaxels outside the region defined by region_string.

        Notes: It uses pyregion package.

        """
        im_aux = np.ones_like(self.white_data)
        hdu_aux = fits.open(self.filename_white)[1]
        hdu_aux.data = im_aux
        r = pyregion.parse(region_string)
        mask_new = r.get_mask(hdu=hdu_aux)
        mask_new_inverse = ~mask_new
        complete_mask_new = mask_new_inverse + self.mask_init
        complete_mask_new = np.where(complete_mask_new != 0, True, False)
        self.draw_pyregion(region_string)
        if _2d:
            return mask_new_inverse
        return complete_mask_new

    def plot_sextractor_regions(self, sextractor_filename, flag_threshold=32):
        self.clean_canvas()
        x_pix = np.array(self.get_from_table(sextractor_filename, 'X_IMAGE'))
        y_pix = np.array(self.get_from_table(sextractor_filename, 'Y_IMAGE'))
        a = np.array(self.get_from_table(sextractor_filename, 'A_IMAGE'))
        b = np.array(self.get_from_table(sextractor_filename, 'B_IMAGE'))
        theta = np.array(self.get_from_table(sextractor_filename, 'THETA_IMAGE'))
        flags = self.get_from_table(sextractor_filename, 'FLAGS')
        id = self.get_from_table(sextractor_filename, 'NUMBER')
        mag = self.get_from_table(sextractor_filename, 'MAG_AUTO')
        n = len(x_pix)
        for i in xrange(n):
            color = 'Green'
            if flags[i] > flag_threshold:
                color = 'Red'
            region_string = self.ellipse_param_to_ds9reg_string(x_pix[i], y_pix[i], a[i], b[i], theta[i], color=color)
            self.draw_pyregion(region_string)
            plt.text(x_pix[i], y_pix[i], id[i], color='Red')
        return x_pix, y_pix, a, b, theta, flags, id, mag

    def save_sextractor_specs(self, sextractor_filename, flag_threshold=32, redmonster_format=True, n_figure=2,
                              mode='ivar', mag_kwrd='mag_r'):
        x_pix, y_pix, a, b, theta, flags, id, mag = self.plot_sextractor_regions(
            sextractor_filename=sextractor_filename,
            flag_threshold=flag_threshold)
        self.clean_canvas()
        n = len(x_pix)
        for i in xrange(n):
            if flags[i] <= flag_threshold:
                x_world, y_world = self.p2w(x_pix[i], y_pix[i])
                coord = SkyCoord(ra=x_world, dec=y_world, frame='icrs', unit='deg')
                spec_fits_name = name_from_coord(coord)
                spec = self.get_spec_from_ellipse_params(x_c=x_pix[i], y_c=y_pix[i], params=[a[i], b[i], theta[i]],
                                                         mode=mode, save=False, n_figure=n_figure)
                str_id = str(id[i]).zfill(3)
                spec_fits_name = str_id + '_' + spec_fits_name
                if redmonster_format:
                    self.spec_to_redmonster_format(spec=spec, fitsname=spec_fits_name + '_RMF.fits', n_id=id[i],
                                                   mag=[mag_kwrd, mag[i]])
                else:
                    spec.write_to_fits(spec_fits_name + '.fits')
                    hdulist = fits.open(spec_fits_name + '.fits')
                    hdulist[0].header[mag_kwrd] = mag[i]
                    hdulist.writeto(spec_fits_name + '.fits', clobber=True)

    def __read_files(self, input):
        path = input
        files = glob.glob(path)
        return files

    def create_movie_wavelength_range(self, initial_wavelength, final_wavelength, width=5., outvid='image_video.avi',
                                      erase=True):
        """
        Function to create a film over a wavelength range of the cube
        :param initial_wavelength: initial wavelength of the film
        :param final_wavelength:  final wavelength of the film
        :param width: width of the wavelength range in each frame
        :param outvid: name of the final video
        :param erase: if True, the individual frames will be erased after producing the video
        :return:
        """
        wave = self.create_wavelength_array()
        n = len(wave)
        index_ini = int(self.closest_element(wave, initial_wavelength))
        if initial_wavelength < wave[0]:
            print str(
                initial_wavelength) + ' es menor al limite inferior minimo permitido, se usara en su lugar ' + str(
                wave[0])
            initial_wavelength = wave[0]
        if final_wavelength > wave[n - 1]:
            print str(final_wavelength) + ' es mayor al limite superior maximo permitido, se usara en su lugar ' + str(
                wave[n - 1])
            final_wavelength = wave[n - 1]

        images_names = []
        fitsnames = []
        for i in xrange(initial_wavelength, final_wavelength):
            wavelength_range = (i, i + width)
            filename = 'colapsed_image_' + str(i) + '_'
            im = self.get_image(wv_inpu=wavelength_range, fitsname=filename + '.fits', type='sum', save='True')
            plt.close(15)
            image = aplpy.FITSFigure(filename + '.fits', figure=plt.figure(15))
            image.show_grayscale(vmin=self.vmin, vmax=self.vmax)
            image.save(filename=filename + '.png')
            fitsnames.append(filename + '.fits')
            images_names.append(filename + '.png')
            plt.close(15)
        video = self.make_video(images=images_names, outvid=outvid)
        n_im = len(fitsnames)
        if erase:
            for i in xrange(n_im):
                fits_im = fitsnames[i]
                png_im = images_names[i]
                command_fits = 'rm ' + fits_im
                command_png = 'rm ' + png_im
                os.system(command_fits)
                os.system(command_png)
        return video

    def find_wv_inds(self, wv_array):
        """

        :param wv_array
        :return: Returns the indices in the cube, that are closest to wv_array
        """
        inds = [np.argmin(np.fabs(wv_ii - self.wavelength)) for wv_ii in wv_array]
        inds = np.unique(inds)
        return inds

    def sub_cube(self, wv_input):
        """
        Returns a cube-like object with fewer wavelength elements

        :param wv_input: tuple or np.array
        :return: XXXX
        """
        if isinstance(wv_input, tuple):
            if len(wv_input) != 2:
                raise ValueError(
                    "If wv_input is given as tuple, it must be of lenght = 2, interpreted as (wv_min, wv_max)")
            wv_inds = self.find_wv_inds(wv_input)
            ind_min = np.min(wv_inds)
            ind_max = np.max(wv_inds)
            sub_cube = self.cube[ind_min:ind_max, :, :]
        else:  # assuming array-like for wv_input
            wv_inds = self.find_wv_inds(wv_input)
            sub_cube = self.cube[wv_inds, :, :]
        return sub_cube

    def get_filtered_image(self, _filter='r', save=True, n_figure=5):
        """
        Function used to produce a filtered image from the cube
        :param _filter: string, default = r
                        possible values: u,g,r,i,z , sdss filter to get the new image
        :param save: Boolean, default = True
                     If True, the image will be saved
        :return:
        """

        w = self.create_wavelength_array()
        filter_curve = self.get_filter(wavelength_spec=w, _filter=_filter)
        condition = np.where(filter_curve > 0)[0]
        fitsname = 'new_image_' + _filter + '_filter.fits'
        sub_cube = self.cube[condition]
        filter_curve_final = filter_curve[condition]
        extra_dims = sub_cube.ndim - filter_curve_final.ndim
        new_shape = filter_curve_final.shape + (1,) * extra_dims
        new_filter_curve = filter_curve_final.reshape(new_shape)
        new_filtered_cube = sub_cube * new_filter_curve
        new_filtered_image = np.sum(new_filtered_cube, axis=0)
        if save:
            self.__save2fits(fitsname, new_filtered_image.data, type='white', n_figure=n_figure)
        return new_filtered_image

    def get_image(self, wv_input, fitsname='new_collapsed_cube.fits', type='sum', n_figure=2, save=False):
        """
        Function used to colapse a determined wavelength range in a sum or a median type
        :param wv_input: tuple or list
                         can be a list of wavelengths or a tuple that will represent a  range
        :param fitsname: str
                         The name of the fits that will contain the new image
        :param type: str, possible values: 'sum' or 'median'
                     The type of combination that will be done
        :param n_figure: int
                         Figure to display the new image if it is saved
        :return:
        """
        sub_cube = self.sub_cube(wv_input)
        if type == 'sum':
            matrix_flat = np.sum(sub_cube, axis=0)
        elif type == 'median':
            matrix_flat = np.median(sub_cube, axis=0)
        else:
            raise ValueError('Unknown type, please chose sum or median')

        if save:
            self.__save2fits(fitsname, matrix_flat.data, type='white', n_figure=n_figure)
        return matrix_flat

    def get_continuum_range(self, range):
        """

        :param range: tuple
                      contain the range of a emission line. Continuum will be computed around this range
        :return: cont_range_inf: The continuum range at the left of the Emission line, same length than input range
                 cont_range_sup: The continuum range at the right of the Emission line, same length than input range
                 n             : The number of element in the wavelength space inside the ranges
        """
        wv_inds = self.find_wv_inds(range)
        n = len(wv_inds)
        wv_inds_sup = wv_inds + n
        wv_inds_inf = wv_inds - n
        cont_range_inf = (wv_inds_inf[0], wv_inds_inf[n - 1])
        cont_range_sup = (wv_inds_sup[0], wv_inds_sup[n - 1])
        return cont_range_inf, cont_range_sup, n

    def get_image_wv_ranges(self, wv_ranges, substract_cont=True, fitsname='new_collapsed_cube.fits', save=False,
                            n_figure=3):
        image_stacker = np.zeros_like(self.white_data)
        for r in wv_ranges:
            image = self.get_image(r)
            cont_range_inf, cont_range_sup, n = self.get_continuum_range(r)
            cont_inf_image = self.get_image(cont_range_inf, type='median')
            cont_sup_image = self.get_image(cont_range_sup, type='median')
            cont_image = n * (cont_inf_image + cont_sup_image) / 2.
            if substract_cont:
                image = image - cont_image
            image_stacker = image_stacker + image.data

        if save:
            self.__save2fits(fitsname, image_stacker, type='white', n_figure=n_figure)
        return image_stacker

    def create_white(self, new_white_fitsname='white_from_colapse.fits'):
        """
        Function that collapses all wavelengths available to produce a new white image
        :param new_white_fitsname: Name of the new image
        :return:
        """
        wave = self.create_wavelength_array()
        n = len(wave)
        self.get_image((wave[0], wave[n - 1]), fitsname=new_white_fitsname, save=True)

    def spec_to_redmonster_format(self, spec, fitsname, n_id=None, mag=None):
        """
        Function used to create a spectrum in the REDMONSTER software format
        :param spec: XSpectrum1D object
        :param mag: List containing 2 elements, the first is the keyword in the header that will contain the magnitud saved in the second element
        :param fitsname:  Name of the fitsfile that will be created
        :return:
        """
        from scipy import interpolate
        wave = spec.wavelength.value
        wave_log = np.log10(wave)
        n = len(wave)
        spec.wavelength = wave_log * u.angstrom
        new_wave_log = np.arange(wave_log[1], wave_log[n - 2], 0.0001)
        spec_rebined = spec.rebin(new_wv=new_wave_log * u.angstrom)
        flux = spec_rebined.flux.value
        f = interpolate.interp1d(wave_log, spec.sig.value)
        sig = f(new_wave_log)
        inv_sig = 1. / np.array(sig) ** 2
        inv_sig = np.where(np.isinf(inv_sig), 0, inv_sig)
        inv_sig = np.where(np.isnan(inv_sig), 0, inv_sig)
        hdu1 = fits.PrimaryHDU([flux])
        hdu2 = fits.ImageHDU([inv_sig])
        hdu1.header['COEFF0'] = new_wave_log[0]
        hdu1.header['COEFF1'] = new_wave_log[1] - new_wave_log[0]
        if n_id != None:
            hdu1.header['ID'] = n_id
        if mag != None:
            hdu1.header[mag[0]] = mag[1]
        hdulist_new = fits.HDUList([hdu1, hdu2])
        hdulist_new.writeto(fitsname, clobber=True)

    def calculate_mag(self, wavelength, flux, _filter, zeropoint_flux=9.275222661263278e-07):
        dw = np.diff(wavelength)
        new_flux = flux * _filter
        f_mean = (new_flux[:-1] + new_flux[1:]) * 0.5
        total_flux = np.sum(f_mean * dw) * self.flux_units.value
        mag = -2.5 * np.log10(total_flux / zeropoint_flux)
        return mag

    def get_filter(self, wavelength_spec, _filter='r'):

        wave_u = np.arange(2980, 4155, 25)

        wave_g = np.arange(3630, 5855, 25)

        wave_r = np.arange(5380, 7255, 25)

        wave_i = np.arange(6430, 8655, 25)

        wave_z = np.arange(7730, 11255, 25)

        flux_u = np.array(
            [0.00000000e+00, 1.00000000e-04, 5.00000000e-04, 1.30000000e-03, 2.60000000e-03, 5.20000000e-03,
             9.30000000e-03, 1.61000000e-02, 2.40000000e-02, 3.23000000e-02, 4.05000000e-02, 4.85000000e-02,
             5.61000000e-02, 6.34000000e-02, 7.00000000e-02, 7.56000000e-02, 8.03000000e-02, 8.48000000e-02,
             8.83000000e-02, 9.17000000e-02, 9.59000000e-02, 1.00100000e-01, 1.02900000e-01, 1.04400000e-01,
             1.05300000e-01, 1.06300000e-01, 1.07500000e-01, 1.08500000e-01, 1.08400000e-01, 1.06400000e-01,
             1.02400000e-01, 9.66000000e-02, 8.87000000e-02, 7.87000000e-02, 6.72000000e-02, 5.49000000e-02,
             4.13000000e-02, 2.68000000e-02, 1.45000000e-02, 7.50000000e-03, 4.20000000e-03, 2.20000000e-03,
             1.00000000e-03, 6.00000000e-04, 4.00000000e-04, 2.00000000e-04, 0.00000000e+00])

        flux_g = np.array(
            [0.00000000e+00, 3.00000000e-04, 8.00000000e-04,
             1.30000000e-03, 1.90000000e-03, 2.40000000e-03,
             3.40000000e-03, 5.50000000e-03, 1.03000000e-02,
             1.94000000e-02, 3.26000000e-02, 4.92000000e-02,
             6.86000000e-02, 9.00000000e-02, 1.12300000e-01,
             1.34200000e-01, 1.54500000e-01, 1.72200000e-01,
             1.87300000e-01, 2.00300000e-01, 2.11600000e-01,
             2.21400000e-01, 2.30100000e-01, 2.37800000e-01,
             2.44800000e-01, 2.51300000e-01, 2.57400000e-01,
             2.63300000e-01, 2.69100000e-01, 2.74700000e-01,
             2.80100000e-01, 2.85200000e-01, 2.89900000e-01,
             2.94000000e-01, 2.97900000e-01, 3.01600000e-01,
             3.05500000e-01, 3.09700000e-01, 3.14100000e-01,
             3.18400000e-01, 3.22400000e-01, 3.25700000e-01,
             3.28400000e-01, 3.30700000e-01, 3.32700000e-01,
             3.34600000e-01, 3.36400000e-01, 3.38300000e-01,
             3.40300000e-01, 3.42500000e-01, 3.44800000e-01,
             3.47200000e-01, 3.49500000e-01, 3.51900000e-01,
             3.54100000e-01, 3.56200000e-01, 3.58100000e-01,
             3.59700000e-01, 3.60900000e-01, 3.61300000e-01,
             3.60900000e-01, 3.59500000e-01, 3.58100000e-01,
             3.55800000e-01, 3.45200000e-01, 3.19400000e-01,
             2.80700000e-01, 2.33900000e-01, 1.83900000e-01,
             1.35200000e-01, 9.11000000e-02, 5.48000000e-02,
             2.95000000e-02, 1.66000000e-02, 1.12000000e-02,
             7.70000000e-03, 5.00000000e-03, 3.20000000e-03,
             2.10000000e-03, 1.50000000e-03, 1.20000000e-03,
             1.00000000e-03, 9.00000000e-04, 8.00000000e-04,
             6.00000000e-04, 5.00000000e-04, 3.00000000e-04,
             1.00000000e-04, 0.00000000e+00])

        flux_r = np.array(
            [0.00000000e+00, 1.40000000e-03, 9.90000000e-03,
             2.60000000e-02, 4.98000000e-02, 8.09000000e-02,
             1.19000000e-01, 1.63000000e-01, 2.10000000e-01,
             2.56400000e-01, 2.98600000e-01, 3.33900000e-01,
             3.62300000e-01, 3.84900000e-01, 4.02700000e-01,
             4.16500000e-01, 4.27100000e-01, 4.35300000e-01,
             4.41600000e-01, 4.46700000e-01, 4.51100000e-01,
             4.55000000e-01, 4.58700000e-01, 4.62400000e-01,
             4.66000000e-01, 4.69200000e-01, 4.71600000e-01,
             4.73100000e-01, 4.74000000e-01, 4.74700000e-01,
             4.75800000e-01, 4.77600000e-01, 4.80000000e-01,
             4.82700000e-01, 4.85400000e-01, 4.88100000e-01,
             4.90500000e-01, 4.92600000e-01, 4.94200000e-01,
             4.95100000e-01, 4.95500000e-01, 4.95600000e-01,
             4.95800000e-01, 4.96100000e-01, 4.96400000e-01,
             4.96200000e-01, 4.95300000e-01, 4.93100000e-01,
             4.90600000e-01, 4.87300000e-01, 4.75200000e-01,
             4.47400000e-01, 4.05900000e-01, 3.54400000e-01,
             2.96300000e-01, 2.35000000e-01, 1.73900000e-01,
             1.16800000e-01, 6.97000000e-02, 3.86000000e-02,
             2.15000000e-02, 1.36000000e-02, 1.01000000e-02,
             7.70000000e-03, 5.60000000e-03, 3.90000000e-03,
             2.80000000e-03, 2.00000000e-03, 1.60000000e-03,
             1.30000000e-03, 1.00000000e-03, 7.00000000e-04,
             4.00000000e-04, 2.00000000e-04, 0.00000000e+00])

        flux_i = np.array(
            [0.00000000e+00, 1.00000000e-04, 3.00000000e-04,
             4.00000000e-04, 4.00000000e-04, 4.00000000e-04,
             3.00000000e-04, 4.00000000e-04, 9.00000000e-04,
             1.90000000e-03, 3.40000000e-03, 5.60000000e-03,
             1.04000000e-02, 1.97000000e-02, 3.49000000e-02,
             5.69000000e-02, 8.51000000e-02, 1.18100000e-01,
             1.55200000e-01, 1.98000000e-01, 2.44800000e-01,
             2.90600000e-01, 3.29000000e-01, 3.56600000e-01,
             3.82900000e-01, 4.06700000e-01, 4.24500000e-01,
             4.32000000e-01, 4.25200000e-01, 4.02800000e-01,
             3.84400000e-01, 3.91100000e-01, 4.01100000e-01,
             3.98800000e-01, 3.92400000e-01, 3.91900000e-01,
             3.98800000e-01, 3.97900000e-01, 3.93000000e-01,
             3.89800000e-01, 3.87200000e-01, 3.84200000e-01,
             3.79900000e-01, 3.73700000e-01, 3.68500000e-01,
             3.67800000e-01, 3.60300000e-01, 1.52700000e-01,
             2.17600000e-01, 2.75200000e-01, 3.43400000e-01,
             3.39200000e-01, 3.36100000e-01, 3.31900000e-01,
             3.27200000e-01, 3.22100000e-01, 3.17300000e-01,
             3.12900000e-01, 3.09500000e-01, 3.07700000e-01,
             3.07500000e-01, 3.08600000e-01, 3.09800000e-01,
             3.09800000e-01, 3.07600000e-01, 3.02100000e-01,
             2.93900000e-01, 2.82100000e-01, 2.59700000e-01,
             2.24200000e-01, 1.81500000e-01, 1.37400000e-01,
             9.73000000e-02, 6.52000000e-02, 4.10000000e-02,
             2.37000000e-02, 1.28000000e-02, 7.40000000e-03,
             5.30000000e-03, 3.60000000e-03, 2.20000000e-03,
             1.40000000e-03, 1.10000000e-03, 1.00000000e-03,
             1.00000000e-03, 9.00000000e-04, 6.00000000e-04,
             3.00000000e-04, 0.00000000e+00])

        flux_z = np.array(
            [0., 0., 0.0001, 0.0001, 0.0001, 0.0002, 0.0002,
             0.0003, 0.0005, 0.0007, 0.0011, 0.0017, 0.0027, 0.004,
             0.0057, 0.0079, 0.0106, 0.0139, 0.0178, 0.0222, 0.0271,
             0.0324, 0.0382, 0.0446, 0.0511, 0.0564, 0.0603, 0.0637,
             0.0667, 0.0694, 0.0717, 0.0736, 0.0752, 0.0765, 0.0775,
             0.0782, 0.0786, 0.0787, 0.0785, 0.078, 0.0772, 0.0763,
             0.0751, 0.0738, 0.0723, 0.0708, 0.0693, 0.0674, 0.0632,
             0.0581, 0.0543, 0.0526, 0.0523, 0.0522, 0.0512, 0.0496,
             0.0481, 0.0473, 0.0476, 0.0482, 0.0476, 0.0447, 0.0391,
             0.0329, 0.0283, 0.0264, 0.0271, 0.0283, 0.0275, 0.0254,
             0.0252, 0.0256, 0.0246, 0.0244, 0.0252, 0.0258, 0.0265,
             0.0274, 0.0279, 0.0271, 0.0252, 0.0236, 0.0227, 0.0222,
             0.0216, 0.0208, 0.0196, 0.0183, 0.0171, 0.016, 0.0149,
             0.0138, 0.0128, 0.0118, 0.0108, 0.0099, 0.0091, 0.0083,
             0.0075, 0.0068, 0.0061, 0.0055, 0.005, 0.0045, 0.0041,
             0.0037, 0.0033, 0.003, 0.0027, 0.0025, 0.0023, 0.0021,
             0.0019, 0.0018, 0.0017, 0.0016, 0.0015, 0.0014, 0.0013,
             0.0012, 0.0011, 0.001, 0.0009, 0.0008, 0.0008, 0.0007,
             0.0006, 0.0006, 0.0006, 0.0005, 0.0005, 0.0004, 0.0004,
             0.0003, 0.0003, 0.0002, 0.0002, 0.0001, 0.0001, 0., 0.])
        if _filter == 'u':
            wave_filter = wave_u
            flux_filter = flux_u
        if _filter == 'g':
            wave_filter = wave_g
            flux_filter = flux_g
        if _filter == 'r':
            wave_filter = wave_r
            flux_filter = flux_r
        if _filter == 'i':
            wave_filter = wave_i
            flux_filter = flux_i
        if _filter == 'z':
            wave_filter = wave_z
            flux_filter = flux_z
            # filter es una built-in the python, creo que es mejor cambiarlo a ese nombre para evitar confusiones.

        new_filter_wavelength = self.overlap_filter(wave_filter, wavelength_spec)
        interpolator = interpolate.interp1d(wave_filter, flux_filter)
        new_filter_flux = interpolator(new_filter_wavelength)

        final_flux_filter = []

        for j, w in enumerate(wavelength_spec):
            k = self.indexOf(new_filter_wavelength, w)
            if k >= 0:
                final_flux_filter.append(new_filter_flux[k])
            else:
                final_flux_filter.append(0.)
        return np.array(final_flux_filter)

    def overlap_filter(self, wave_filter, wavelength_spec):
        n = len(wave_filter)
        w_min = wave_filter[0]
        w_max = wave_filter[n - 1]
        w_spec_overlap = []
        for w in wavelength_spec:
            if w >= w_min and w <= w_max:
                w_spec_overlap.append(w)
        return np.array(w_spec_overlap)

    def clean_canvas(self):
        """
        Clean everything from the canvas with the colapsed cube image
        :param self:
        :return:
        """
        plt.figure(self.n)
        plt.clf()
        self.gc2 = aplpy.FITSFigure(self.filename_white, figure=plt.figure(self.n))
        self.gc2.show_grayscale(vmin=self.vmin, vmax=self.vmax)
        plt.show()

    def create_table(self, input_file):
        """
        Create a table from a SExtractor output file
        :param input_file: string
                           name of the SExtractor output file
        :return: table
        """
        from astropy.io.ascii.sextractor import SExtractor
        sex = SExtractor()
        table = sex.read(input_file)
        return table

    def get_from_table(self, input_file, keyword):
        """
        Get a columns that correspond to a given keyword from a SExtractor outputfile
        :param input_file: string
                         name of the SExtractor output file

        :param keyword: string
                        keyword in the SExtractor output file
        :return: data
                 the column associated to the keyword
        """
        table = self.create_table(input_file)
        data = table[keyword]
        return data

    def get_weighted_spec(self, x_c=None, y_c=None, params=None,region_string_ = None, coord_system='pix'):
        """
        Function that extract the spectrum from an aperture defined either by elliptical parameters or  by an elliptical region defined by region_string in ds9 format
        :param x_c: x_coordinate of the center of the aperture
        :param y_c: y_coordinate of the center of the aperture
        :param params: Either a single radius or a set of [a,b,theta] params
        :param region_string: region defined by ds9 format
        :param coord_system: in the case of defining and aperture using x_c,y_c,params, must indicate the type of this coordiantes. Possible values: 'pix' and 'wcs'
        :return: XSpectrum1D object
        """
        if max(x_c,y_c,params,region_string_)==None:
            raise ValueError('Not valid input')
        if region_string_ != None:
            x_c,y_c,params = self.params_from_ellipse_region_string(region_string_)


        if not isinstance(params, (int, float, tuple, list, np.array)):
            raise ValueError('Not ready for this `radius` type.')
        if isinstance(params, (int, float)):
            a = params
            b = params
            theta = 0
        elif isiterable(params) and (len(params) == 3):
            a = max(params[:2])
            b = min(params[:2])
            theta = params[2]
        else:
            raise ValueError('If iterable, the length of radius must be == 3; otherwise try float.')
        if coord_system == 'wcs':
            x_center, y_center, params = self.ellipse_params_to_pixel(x_c, y_c, radius=[a, b, theta])
        else:  # already in pixels
            x_center, y_center, params = x_c, y_c, [a, b, theta]
        xc = x_center
        yc = y_center
        from astropy.modeling import models, fitting
        halfsize = [a, b]
        if region_string_==None:
            region_string = self.ellipse_param_to_ds9reg_string(xc, yc, a, b, theta)
        else:
            region_string=region_string_
        new_2dmask = self.create_new_3dmask(region_string, _2d=True)
        masked_white = ma.MaskedArray(self.white_data)
        masked_white.mask = new_2dmask
        ###### Define domain matrix:
        matrix_x = np.zeros_like(self.white_data)
        matrix_y = np.zeros_like(self.white_data)
        n = self.white_data.shape[0]
        m = self.white_data.shape[1]
        for i in xrange(m):
            matrix_x[:, i] = i
        for j in xrange(n):
            matrix_y[j, :] = j
        ###########

        amp_init = masked_white.max()
        stdev_init_x = 0.33 * halfsize[0]
        stdev_init_y = 0.33 * halfsize[1]
        g_init = models.Gaussian2D(x_mean=xc, y_mean=yc, x_stddev=stdev_init_x,
                                   y_stddev=stdev_init_y, amplitude=amp_init, theta=theta)
        fit_g = fitting.LevMarLSQFitter()
        g = fit_g(g_init, matrix_x, matrix_y, masked_white)
        weigths = ma.MaskedArray(g(matrix_x, matrix_y))
        if (g.y_stddev < 0) or (g.x_stddev < 0):
            raise ValueError('Cannot trust the model, please try other imput parameters.')
        w = self.wavelength
        n = len(w)
        fl = np.zeros(n)
        sig = np.zeros(n)
        new_3dmask = self.create_new_3dmask(region_string)
        self.cube.mask = new_3dmask
        for wv_ii in range(n):
            mask = new_3dmask[wv_ii]
            weigths.mask = mask
            n_spaxels = np.sum(mask)
            weigths = weigths / np.sum(weigths)
            fl[wv_ii] = np.sum(self.cube[wv_ii] * weigths) * n_spaxels
            sig[wv_ii] = np.sqrt(np.sum(self.stat[wv_ii] * (weigths ** 2))) * n_spaxels
        self.cube.mask = self.mask_init
        return XSpectrum1D.from_tuple((w, fl, sig))

    def determinate_seeing_from_white(self, xc, yc, halfsize):
        """
        Function used to estimate the observation seeing of an exposure, fitting a gaussian to  a brigth source of the  image
        :param xc: x coordinate in pixels of a bright source
        :param yc: y coordinate  in pixels of a bright source
        :param halfsize: the radius of the area to fit the gaussian
        :return: seeing: float
                         the observational seeing of the image defined as the FWHM of the gaussian
        """
        from astropy.modeling import models, fitting
        hdulist = fits.open(self.filename_white)
        data = hdulist[1].data
        w, h = 2 * halfsize + 1, 2 * halfsize + 1
        matrix_data = [[0 for x in range(w)] for y in range(h)]
        matrix_x = [[0 for x in range(w)] for y in range(h)]
        matrix_y = [[0 for x in range(w)] for y in range(h)]
        for i in xrange(xc - halfsize, xc + halfsize + 1):
            for j in xrange(yc - halfsize, yc + halfsize + 1):
                i2 = i - (xc - halfsize)
                j2 = j - (yc - halfsize)
                matrix_data[j2][i2] = data[j][i]
                matrix_x[j2][i2] = i2
                matrix_y[j2][i2] = j2
        amp_init = np.matrix(matrix_data).max()
        stdev_init = 0.33 * halfsize

        def tie_stddev(model):  # we need this for tying x_std and y_std
            xstddev = model.x_stddev
            return xstddev

        g_init = models.Gaussian2D(x_mean=halfsize + 0.5, y_mean=halfsize + 0.5, x_stddev=stdev_init,
                                   y_stddev=stdev_init, amplitude=amp_init, tied={'y_stddev': tie_stddev})

        fit_g = fitting.LevMarLSQFitter()
        g = fit_g(g_init, matrix_x, matrix_y, matrix_data)
        if (g.y_stddev < 0) or (g.y_stddev > halfsize):
            raise ValueError('Cannot trust the model, please try other imput parameters.')

        seeing = 2.355 * g.y_stddev * self.pixelsize.to('arcsec')  # in arcsecs
        print 'FWHM={:.2f}'.format(seeing)
        return seeing

    def w2p(self, xw, yw):
        """
        Transform from wcs coordinates system to pixel coordinates

        :param self:
        :param xw: float
                   x coordinate in wcs
        :param yw: float
                   y coordinate in wcs
        :return: xpix: float
                       x coordinate in pixels
                 ypix: float
                       y coordinate in pixels
        """
        xpix, ypix = self.gc.world2pixel(xw, yw)
        if xpix < 0:
            xpix = 0
        if ypix < 0:
            ypix = 0
        return xpix, ypix

    def p2w(self, xp, yp):
        """
        Transform from pixel coordinate system to wcs coordinates

        :param self:
        :param xp: float
                   x coordinate in pixels
        :param yp: float
                   y coordinate in pixels
        :return: xw: float
                     x coordinate in wcs
                 yw: float
                     y coordinate in wcs
        """
        xw, yw = self.gc.pixel2world(xp, yp)
        return xw, yw

    def xyr_to_pixel(self, x_center, y_center, radius):
        """
        Transform the (x,y) center and radius that define a circular region from wcs system coordinate to pixels

        :param self:
        :param x_center: float
                         x coordinate in wcs
        :param y_center: float
                         y coordinate in wcs
        :param radius: float
                       radius of the circular region
        :return: x_center_pix: float
                               x coordinate in pixels
                 y_center_pix: float
                               y coordinate in pixels
                 radius_pix: float
                             radius of the circular region in pixels
        """
        x_r = x_center + radius
        x_r_pix, y_center_pix = self.w2p(x_r, y_center)
        x_center, y_center = self.w2p(x_center, y_center)
        radius = abs(x_r_pix - x_center)
        x_center = int(round(x_center))
        y_center = int(round(y_center))
        radius = int(round(radius + 1))
        x_center_pix = x_center
        y_center_pix = y_center
        radius_pix = radius
        return x_center_pix, y_center_pix, radius_pix

    def size(self):
        """
        Print the dimension size (x,y,lambda) of the datacube.
        :param self:
        :return:
        """
        input = self.filename
        hdulist = fits.open(input)
        hdulist.info()
        self.cube.data.shape

    def substring(self, string, i, j):
        """
        Obtain a the substring of string, from index i to index j, both included

        :param self:
        :param string: string
                       input string
        :param i: int
                  initial index
        :param j: int
                  final index
        :return: out: string
                      output substring
        """
        out = ''
        for k in xrange(i, j + 1):
            out = out + string[k]
        return out

    def create_movie_redshift_range(self, z_ini=0., z_fin=1., dz=0.001, outvid='emission_lines_video.avi', erase=True):
        """
        Function to create a film, colapsing diferent wavelength ranges in which some strong emission lines would fall at certain redshifts
        :param z_ini: initial redshift
        :param z_fin: final redshift
        :param dz: delta redshift
        :param outvid: name of the final video
        :param erase: If true, the individual frames to make the video will be erased after the video is produced
        :return:
        """
        OII = 3728.483
        wave = self.create_wavelength_array()
        n = len(wave)
        w_max = wave[n - 1]
        max_z_allowed = (w_max / OII) - 1.
        if z_fin > max_z_allowed:
            print 'maximum redshift allowed is ' + str(max_z_allowed) + ', this value will be used  instead of ' + str(
                z_fin)
            z_fin = max_z_allowed
        z_array = np.arange(z_ini, z_fin, dz)
        images_names = []
        fitsnames = []
        for z in z_array:
            print 'z = ' + str(z)
            ranges = self.create_ranges(z)
            filename = 'emission_linea_image_redshif_' + str(z) + '_'
            image = self.get_image_wv_ranges(wv_ranges=ranges, fitsname=filename + '.fits', save=True)
            plt.close(15)
            image = aplpy.FITSFigure(filename + '.fits', figure=plt.figure(15))
            image.show_grayscale(vmin=self.vmin, vmax=self.vmax)
            plt.title('Emission lines image at z = ' + str(z))
            image.save(filename=filename + '.png')
            images_names.append(filename + '.png')
            fitsnames.append(filename + '.fits')
            plt.close(15)
        video = self.make_video(images=images_names, outvid=outvid)
        n_im = len(fitsnames)
        if erase:
            for i in xrange(n_im):
                fits_im = fitsnames[i]
                png_im = images_names[i]
                command_fits = 'rm ' + fits_im
                command_png = 'rm ' + png_im
                os.system(command_fits)
                os.system(command_png)
        return video

    def colapse_emission_lines_image(self, nsigma=2, fitsname='colapsed_emission_image.fits'):
        """
        Function used to colapse only wavelength bins in which the signal to noise is greater that nsigma value. This will create a new white image
        :param nsigma: float
                       threshold to signal to noise
        :param fitsname: string
                         name of the new image
        :return:
        """
        raise NotImplementedError()

    def create_ranges(self, z, width=5.):
        """
        Function used to create the wavelength ranges around strong emission lines at a given redshift
        :param z: redshift
        :param width: width  in pixels of the emission lines
        :return:
        """
        wave = self.create_wavelength_array()
        n = len(wave)
        w_max = wave[n - 1]
        w_min = wave[0]
        half = width / 2.
        OII = 3728.483
        Hb = 4862.683
        Ha = 6564.613
        OIII_4959 = 4960.295
        OIII_5007 = 5008.239
        range_OII = np.array([OII - half, OII + half])
        range_Hb = np.array([Hb - half, Hb + half])
        range_Ha = np.array([Ha - half, Ha + half])
        range_OIII_4959 = np.array([OIII_4959 - half, OIII_4959 + half])
        range_OIII_5007 = np.array([OIII_5007 - half, OIII_5007 + half])
        ranges = [range_Ha, range_Hb, range_OII, range_OIII_4959, range_OIII_5007]
        z_ranges = []
        for range in ranges:
            z_ranges.append(range * (1 + z))
        output_ranges = []
        for range in z_ranges:
            if range[0] >= w_min and range[1] <= w_max:
                output_ranges.append(range)
        return output_ranges

    def make_video(self, images, outimg=None, fps=2, size=None, is_color=True, format="XVID", outvid='image_video.avi'):
        from cv2 import VideoWriter, VideoWriter_fourcc, imread, resize
        fourcc = VideoWriter_fourcc(*format)
        vid = None
        for image in images:
            if not os.path.exists(image):
                raise FileNotFoundError(image)
            img = imread(image)
            if vid is None:
                if size is None:
                    size = img.shape[1], img.shape[0]
                vid = VideoWriter(outvid, fourcc, float(fps), size, is_color)
            if size[0] != img.shape[1] and size[1] != img.shape[0]:
                img = resize(img, size)
            vid.write(img)
        vid.release()
        return vid

        # Un radio de 4 pixeles es equivalente a un radio de 0.0002 en wcs
