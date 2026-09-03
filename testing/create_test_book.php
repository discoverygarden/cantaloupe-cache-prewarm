<?php
use Drupal\node\Entity\Node;
use Drupal\media\Entity\Media;
use Drupal\file\Entity\File;
use Drupal\Core\File\FileSystemInterface;

$source_fid = 4; // Cosmographia Petri Apiani 00b2v.jpg
$source_file = File::load($source_fid);
$source_uri = $source_file->getFileUri();

$parent = Node::create([
  'type' => 'islandora_object',
  'title' => 'IIIF Cache Test Book',
  'field_model' => ['target_id' => 480], // Paged Content
  'status' => 1,
]);
$parent->save();
echo "Parent node: " . $parent->id() . PHP_EOL;

$dir = 'public://iiif-cache-test';
\Drupal::service('file_system')->prepareDirectory($dir, FileSystemInterface::CREATE_DIRECTORY);

for ($i = 1; $i <= 5; $i++) {
  $page = Node::create([
    'type' => 'islandora_object',
    'title' => 'Page ' . $i,
    'field_model' => ['target_id' => 481], // Page
    'field_member_of' => ['target_id' => $parent->id()],
    'field_weight' => $i,
    'status' => 1,
  ]);
  $page->save();

  $dest_uri = \Drupal::service('file_system')->getDestinationFilename($dir . '/page-' . $i . '.jpg', FileSystemInterface::EXISTS_RENAME);
  \Drupal::service('file_system')->copy($source_uri, $dest_uri);
  $file = File::create([
    'uri' => $dest_uri,
    'status' => 1,
  ]);
  $file->save();

  $media = Media::create([
    'bundle' => 'image',
    'name' => 'Page ' . $i . ' - Service File',
    'field_media_image' => ['target_id' => $file->id()],
    'field_media_of' => ['target_id' => $page->id()],
    'field_media_use' => ['target_id' => 472], // Service File
    'status' => 1,
  ]);
  $media->save();
  echo "Page $i node: " . $page->id() . " media: " . $media->id() . " file: " . $dest_uri . PHP_EOL;
}
