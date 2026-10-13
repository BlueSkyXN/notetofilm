DIMENSIONS = {'16:9': (1920, 1080), '9:16': (1080, 1920)}


def video_layout(aspect_ratio='16:9', image_fit='contain'):
    if aspect_ratio not in DIMENSIONS or image_fit not in ('contain', 'cover'):
        raise ValueError('画幅或图片适配方式无效')
    width, height = DIMENSIONS[aspect_ratio]
    return {'aspect_ratio': aspect_ratio, 'image_fit': image_fit, 'width': width, 'height': height}
