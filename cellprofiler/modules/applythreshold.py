"""
<b>Apply Threshold</b> sets pixel intensities below or above a certain threshold to zero
<hr>
<b>ApplyThreshold</b> produces a grayscale image based on a threshold which can be
pre-selected or calculated automatically using one of many methods.
"""

import centrosome.cpmorphology
import centrosome.threshold
import scipy.ndimage.morphology

import cellprofiler.image
import cellprofiler.module
import cellprofiler.modules.identify
import cellprofiler.setting
import identify

GRAYSCALE = "Grayscale"
BINARY = "Binary (black and white)"

TH_BELOW_THRESHOLD = "Below threshold"
TH_ABOVE_THRESHOLD = "Above threshold"

'''# of non-threshold settings in current revision'''
N_SETTINGS = 2


class ApplyThreshold(identify.Identify):
    module_name = "ApplyThreshold"
    variable_revision_number = 8
    category = "Image Processing"

    def create_settings(self):
        self.image_name = cellprofiler.setting.ImageNameSubscriber(
            "Select the input image",
            doc="Choose the image to be thresholded."
        )

        self.thresholded_image_name = cellprofiler.setting.ImageNameProvider(
            "Name the output image",
            "ThreshBlue",
            doc="Enter a name for the thresholded image."
        )

        self.create_threshold_settings()

        self.threshold_smoothing_scale.value = 0

    def visible_settings(self):
        return [
            self.image_name,
            self.thresholded_image_name
        ] + self.get_threshold_visible_settings()

    def settings(self):
        return [
            self.image_name,
            self.thresholded_image_name
        ] + self.get_threshold_settings()

    def help_settings(self):
        """Return all settings in a consistent order"""
        return [
            self.image_name,
            self.thresholded_image_name
        ] + self.get_threshold_help_settings()

    def run(self, workspace):
        input = workspace.image_set.get_image(self.image_name.value, must_be_grayscale=True)

        binary_image, local_thresh = self.threshold_image(self.image_name.value, workspace, wants_local_threshold=True)

        pixels = binary_image & input.mask

        output = cellprofiler.image.Image(pixels, parent_image=input)

        workspace.image_set.add(self.thresholded_image_name.value, output)

        if self.show_window:
            workspace.display_data.input_pixel_data = input.pixel_data
            workspace.display_data.output_pixel_data = output.pixel_data
            statistics = workspace.display_data.statistics = []
            workspace.display_data.col_labels = ("Feature", "Value")

            for column in self.get_measurement_columns(workspace.pipeline):
                value = workspace.measurements.get_current_image_measurement(column[1])
                statistics += [(column[1].split('_')[1], str(value))]

    def display(self, workspace, figure):
        figure.set_subplots((3, 1))

        figure.subplot_imshow_grayscale(0, 0, workspace.display_data.input_pixel_data,
                                        title="Original image: %s" %
                                              self.image_name.value)

        figure.subplot_imshow_grayscale(1, 0, workspace.display_data.output_pixel_data,
                                        title="Thresholded image: %s" %
                                              self.thresholded_image_name.value,
                                        sharexy=figure.subplot(0, 0))
        figure.subplot_table(
                2, 0, workspace.display_data.statistics,
                workspace.display_data.col_labels)

    def get_measurement_objects_name(self):
        '''Return the name of the "objects" used to name thresholding measurements

        In the case of ApplyThreshold, we use the image name to name the
        measurements, so the code here works, but is misnamed.
        '''
        return self.thresholded_image_name.value

    def get_measurement_columns(self, pipeline):
        return cellprofiler.modules.identify.get_threshold_measurement_columns(self.thresholded_image_name.value)

    def get_categories(self, pipeline, object_name):
        return self.get_threshold_categories(pipeline, object_name)

    def get_measurements(self, pipeline, object_name, category):
        return self.get_threshold_measurements(pipeline, object_name, category)

    def get_measurement_images(self, pipeline, object_name, category, measurement):
        return self.get_threshold_measurement_objects(
                pipeline, object_name, category, measurement)

    def upgrade_settings(self, setting_values, variable_revision_number, module_name, from_matlab):
        if from_matlab:
            raise NotImplementedError("There is no automatic upgrade path for this module from MatLab pipelines.")

        if variable_revision_number < 7:
            raise NotImplementedError("Automatic upgrade for this module is not supported in CellProfiler 3.0.")

        if variable_revision_number == 7:
            setting_values = setting_values[:2] + setting_values[6:]

            variable_revision_number = 8

        setting_values = setting_values[:N_SETTINGS] + self.upgrade_threshold_settings(setting_values[N_SETTINGS:])

        return setting_values, variable_revision_number, False
