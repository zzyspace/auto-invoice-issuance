#import <Foundation/Foundation.h>
#import <Vision/Vision.h>
#import <ImageIO/ImageIO.h>

static NSArray<VNBarcodeObservation *> *readCodes(NSString *path, NSUInteger scale,
                                                 CGRect *content, NSError **error) {
    CGImageSourceRef source = CGImageSourceCreateWithURL((__bridge CFURLRef)[NSURL fileURLWithPath:path], NULL);
    if (!source) return nil;
    CGImageRef original = CGImageSourceCreateImageAtIndex(source, 0, NULL);
    CFRelease(source);
    if (!original) return nil;
    size_t imageWidth = CGImageGetWidth(original) * scale;
    size_t imageHeight = CGImageGetHeight(original) * scale;
    size_t padding = 16 * scale;
    size_t width = imageWidth + 2 * padding, height = imageHeight + 2 * padding;
    if (width > 10000 || height > 10000 || width * height > 20000000) {
        CGImageRelease(original);
        return nil;
    }
    CGColorSpaceRef colors = CGColorSpaceCreateDeviceRGB();
    CGContextRef canvas = CGBitmapContextCreate(NULL, width, height, 8, 0, colors, kCGImageAlphaPremultipliedLast);
    CGColorSpaceRelease(colors);
    if (!canvas) { CGImageRelease(original); return nil; }
    CGContextSetRGBFillColor(canvas, 1, 1, 1, 1);
    CGContextFillRect(canvas, CGRectMake(0, 0, width, height));
    CGContextSetInterpolationQuality(canvas, kCGInterpolationNone);
    CGContextDrawImage(canvas, CGRectMake(padding, padding, imageWidth, imageHeight), original);
    CGImageRelease(original);
    CGImageRef image = CGBitmapContextCreateImage(canvas);
    CGContextRelease(canvas);
    if (!image) return nil;
    if (content) *content = CGRectMake((double)padding / width, (double)padding / height,
                                      (double)imageWidth / width, (double)imageHeight / height);
    VNDetectBarcodesRequest *request = [[VNDetectBarcodesRequest alloc] init];
    request.symbologies = @[VNBarcodeSymbologyQR];
    request.usesCPUOnly = YES;
    VNImageRequestHandler *handler = [[VNImageRequestHandler alloc] initWithCGImage:image options:@{}];
    BOOL success = [handler performRequests:@[request] error:error];
    CGImageRelease(image);
    return success ? request.results : nil;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 3) {
            fprintf(stderr, "Expected source QR image and picker screenshot.\n");
            return 1;
        }
        NSError *error = nil;
        NSMutableSet<NSString *> *payloads = [NSMutableSet set];
        for (NSUInteger scale = 1; scale <= 3 && payloads.count == 0; scale++) {
            for (VNBarcodeObservation *code in readCodes(@(argv[1]), scale, NULL, &error)) {
                if (code.payloadStringValue.length) [payloads addObject:code.payloadStringValue];
            }
        }
        if (payloads.count != 1) {
            fprintf(stderr, "Source image must contain one unique readable QR code.\n");
            return 1;
        }
        NSMutableArray *matches = [NSMutableArray array];
        BOOL readScreenshot = NO;
        for (NSUInteger scale = 1; scale <= 3 && matches.count == 0; scale++) {
            CGRect content;
            NSArray<VNBarcodeObservation *> *visible = readCodes(@(argv[2]), scale, &content, &error);
            if (!visible) continue;
            readScreenshot = YES;
            for (VNBarcodeObservation *code in visible) {
                if (![payloads containsObject:code.payloadStringValue ?: @""]) continue;
                CGRect box = code.boundingBox;
                // Undo padding/scale before mapping to the original window image.
                box = CGRectMake((box.origin.x - content.origin.x) / content.size.width,
                                 (box.origin.y - content.origin.y) / content.size.height,
                                 box.size.width / content.size.width, box.size.height / content.size.height);
                box = CGRectIntersection(box, CGRectMake(0, 0, 1, 1));
                if (CGRectIsNull(box) || CGRectIsEmpty(box)) continue;
                double x = MAX(0.0, MIN(1.0, box.origin.x));
                double y = MAX(0.0, MIN(1.0, 1.0 - CGRectGetMaxY(box)));
                [matches addObject:@{@"x": @(x), @"y": @(y),
                                     @"width": @(MIN(box.size.width, 1.0 - x)),
                                     @"height": @(MIN(box.size.height, 1.0 - y))}];
            }
        }
        if (!readScreenshot) {
            fprintf(stderr, "Unable to read picker screenshot.\n");
            return 1;
        }
        // Return geometry only. Login QR payloads must never enter logs or stdout.
        NSData *data = [NSJSONSerialization dataWithJSONObject:@{@"matches": matches} options:0 error:&error];
        if (!data) return 1;
        fwrite(data.bytes, 1, data.length, stdout);
        return 0;
    }
}
