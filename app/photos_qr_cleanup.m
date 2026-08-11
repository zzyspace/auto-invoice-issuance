#import <CommonCrypto/CommonDigest.h>
#import <Foundation/Foundation.h>
#import <Photos/Photos.h>

static int fail(NSString *message) {
    fprintf(stderr, "%s\n", message.UTF8String);
    return EXIT_FAILURE;
}

static int printResult(NSString *status, NSString *assetID) {
    NSError *error = nil;
    NSData *data = [NSJSONSerialization dataWithJSONObject:@{
        @"status": status,
        @"asset_id": assetID,
    } options:NSJSONWritingSortedKeys error:&error];
    if (data == nil) {
        return fail([NSString stringWithFormat:@"Unable to encode cleanup result: %@", error]);
    }
    NSString *output = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    printf("%s\n", output.UTF8String);
    return EXIT_SUCCESS;
}

static PHAuthorizationStatus authorizedStatus(void) {
    __block PHAuthorizationStatus status =
        [PHPhotoLibrary authorizationStatusForAccessLevel:PHAccessLevelReadWrite];
    if (status == PHAuthorizationStatusNotDetermined) {
        dispatch_semaphore_t semaphore = dispatch_semaphore_create(0);
        [PHPhotoLibrary requestAuthorizationForAccessLevel:PHAccessLevelReadWrite
                                                   handler:^(PHAuthorizationStatus newStatus) {
            status = newStatus;
            dispatch_semaphore_signal(semaphore);
        }];
        dispatch_semaphore_wait(semaphore, DISPATCH_TIME_FOREVER);
    }
    return status;
}

static NSData *readOriginalData(PHAssetResource *resource, NSError **resultError) {
    dispatch_semaphore_t semaphore = dispatch_semaphore_create(0);
    NSMutableData *data = [NSMutableData data];
    __block NSError *receivedError = nil;
    PHAssetResourceRequestOptions *options = [[PHAssetResourceRequestOptions alloc] init];
    options.networkAccessAllowed = NO;
    [[PHAssetResourceManager defaultManager]
        requestDataForAssetResource:resource
                            options:options
                dataReceivedHandler:^(NSData *chunk) {
        @synchronized (data) {
            [data appendData:chunk];
        }
    } completionHandler:^(NSError *error) {
        receivedError = error;
        dispatch_semaphore_signal(semaphore);
    }];
    dispatch_semaphore_wait(semaphore, DISPATCH_TIME_FOREVER);
    if (receivedError != nil) {
        if (resultError != NULL) {
            *resultError = receivedError;
        }
        return nil;
    }
    return [data copy];
}

static NSString *sha256(NSData *data) {
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(data.bytes, (CC_LONG)data.length, digest);
    NSMutableString *output = [NSMutableString stringWithCapacity:CC_SHA256_DIGEST_LENGTH * 2];
    for (NSUInteger index = 0; index < CC_SHA256_DIGEST_LENGTH; index++) {
        [output appendFormat:@"%02x", digest[index]];
    }
    return output;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 6) {
            return fail(@"Expected asset ID, original filename, SHA-256, width, and height.");
        }
        NSString *assetID = [NSString stringWithUTF8String:argv[1]];
        NSString *expectedFilename = [NSString stringWithUTF8String:argv[2]];
        NSString *expectedChecksum = [[NSString stringWithUTF8String:argv[3]] lowercaseString];
        NSInteger expectedWidth = [[NSString stringWithUTF8String:argv[4]] integerValue];
        NSInteger expectedHeight = [[NSString stringWithUTF8String:argv[5]] integerValue];

        PHAuthorizationStatus status = authorizedStatus();
        if (status != PHAuthorizationStatusAuthorized) {
            return fail([NSString stringWithFormat:
                @"Photos read/write access is not authorized (status=%ld).", (long)status]);
        }

        PHFetchResult<PHAsset *> *fetched =
            [PHAsset fetchAssetsWithLocalIdentifiers:@[assetID] options:nil];
        if (fetched.count == 0) {
            return printResult(@"already-missing", assetID);
        }
        if (fetched.count != 1) {
            return fail([NSString stringWithFormat:
                @"Safety check failed: expected exactly one asset for the recorded ID, found %lu.",
                (unsigned long)fetched.count]);
        }
        PHAsset *asset = fetched.firstObject;
        if (asset.mediaType != PHAssetMediaTypeImage) {
            return fail(@"Safety check failed: recorded Photos asset is not an image.");
        }
        if (asset.pixelWidth != expectedWidth || asset.pixelHeight != expectedHeight) {
            return fail([NSString stringWithFormat:
                @"Safety check failed: Photos asset dimensions are %lux%lu.",
                (unsigned long)asset.pixelWidth, (unsigned long)asset.pixelHeight]);
        }

        NSMutableArray<PHAssetResource *> *matchingResources = [NSMutableArray array];
        for (PHAssetResource *resource in [PHAssetResource assetResourcesForAsset:asset]) {
            if ([resource.originalFilename isEqualToString:expectedFilename]) {
                [matchingResources addObject:resource];
            }
        }
        if (matchingResources.count != 1) {
            return fail([NSString stringWithFormat:
                @"Safety check failed: expected exactly one resource with the recorded filename, found %lu.",
                (unsigned long)matchingResources.count]);
        }
        NSError *readError = nil;
        NSData *originalData = readOriginalData(matchingResources.firstObject, &readError);
        if (originalData == nil) {
            return fail([NSString stringWithFormat:
                @"Unable to read the original Photos resource: %@", readError.localizedDescription]);
        }
        NSString *actualChecksum = sha256(originalData);
        if (![actualChecksum isEqualToString:expectedChecksum]) {
            return fail([NSString stringWithFormat:
                @"Safety check failed: Photos resource SHA-256 is %@.", actualChecksum]);
        }

        NSError *deleteError = nil;
        BOOL deleted = [[PHPhotoLibrary sharedPhotoLibrary] performChangesAndWait:^{
            [PHAssetChangeRequest deleteAssets:@[asset]];
        } error:&deleteError];
        if (!deleted) {
            return fail([NSString stringWithFormat:
                @"Unable to delete the verified Photos asset: %@", deleteError.localizedDescription]);
        }

        NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:5.0];
        while ([deadline timeIntervalSinceNow] > 0) {
            if ([PHAsset fetchAssetsWithLocalIdentifiers:@[assetID] options:nil].count == 0) {
                return printResult(@"deleted", assetID);
            }
            [NSThread sleepForTimeInterval:0.2];
        }
        return fail(@"Photos accepted the deletion but the asset is still visible by its recorded ID.");
    }
}
